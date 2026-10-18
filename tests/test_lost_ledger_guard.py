"""
Lost-ledger startup guard (chain_ops.guard_lost_ledger).

When the SQLite ledger (hyper.db / ledger.db) is missing or empty but the per-ledger LMDB block store
still holds the chain, a mainnet node must NOT auto-bootstrap (peer snapshot, bootstrap_url download,
sync_from_genesis re-seed) — those recreate tens of GB of SQLite files the operator deleted. It must
exit with LEDGER_LOST_EXIT_CODE (systemd: RestartPreventExitStatus=78) instead. A fresh install (no
block store) and a healthy ledger must be unaffected, and testnet/regnet are never guarded.
No node subprocess, no network.

Run with: python3 -m pytest tests/test_lost_ledger_guard.py -v
"""
import os
import sqlite3
import types

import pytest

import block_store
import chain_ops
import kvstore


class _Log:
    def __init__(self):
        self.lines = []

    def warning(self, m):
        self.lines.append(str(m))

    critical = error = warning

    def info(self, m):
        pass


def _make_node(tmp_path, version="mainnet0022", full_ledger=True):
    node = types.SimpleNamespace()
    node.version = version
    node.full_ledger = full_ledger
    node.ledger_path = os.path.join(str(tmp_path), "ledger.db")
    node.hyper_path = os.path.join(str(tmp_path), "hyper.db")
    node.logger = types.SimpleNamespace(app_log=_Log())
    return node


def _make_blockstore(node, heights=(1, 2)):
    store = block_store.BlockStore(kvstore.store_path(node.ledger_path, "blockstore"), map_size=1 << 24)
    for h in heights:
        store.put_block(h, "%056x" % h, [(1000.0 + h, "addr", "rcpt", "0", "sig%d" % h, "pk", "%056x" % h,
                                          "0", "0", "", "")])
    store.close()


def _make_ledger(path, with_block=True):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, amount, "
                "signature, public_key, block_hash, fee, reward, operation, openfield)")
    if with_block:
        con.execute("INSERT INTO transactions (block_height) VALUES (1)")
    con.commit()
    con.close()


def _placeholder(path):
    """What the live node left behind: an 8 KB SQLite file with only a commit_marker table."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE commit_marker (id INTEGER)")
    con.commit()
    con.close()


def test_deleted_ledger_with_blockstore_exits_with_distinct_code(tmp_path):
    node = _make_node(tmp_path)
    _make_blockstore(node)
    assert chain_ops.lost_ledger_blockstore_tip(node) == 2
    with pytest.raises(SystemExit) as e:
        chain_ops.guard_lost_ledger(node)
    assert e.value.code == chain_ops.LEDGER_LOST_EXIT_CODE == 78
    assert any("Refusing to auto-bootstrap" in l for l in node.logger.app_log.lines)


def test_guard_does_not_create_the_missing_ledger_files(tmp_path):
    node = _make_node(tmp_path)
    _make_blockstore(node)
    with pytest.raises(SystemExit):
        chain_ops.guard_lost_ledger(node)
    assert not os.path.exists(node.ledger_path)
    assert not os.path.exists(node.hyper_path)


def test_placeholder_without_transactions_table_counts_as_lost(tmp_path):
    node = _make_node(tmp_path)
    _make_blockstore(node)
    _placeholder(node.ledger_path)
    _placeholder(node.hyper_path)
    with pytest.raises(SystemExit):
        chain_ops.guard_lost_ledger(node)


def test_only_hyper_lost_in_full_ledger_mode_is_guarded(tmp_path):
    node = _make_node(tmp_path)
    _make_blockstore(node)
    _make_ledger(node.ledger_path)
    assert chain_ops.lost_ledger_blockstore_tip(node) == 2


def test_healthy_ledger_passes(tmp_path):
    node = _make_node(tmp_path)
    _make_blockstore(node)
    _make_ledger(node.ledger_path)
    _make_ledger(node.hyper_path)
    chain_ops.guard_lost_ledger(node)   # no exit


def test_hyperblock_mode_only_needs_hyper(tmp_path):
    node = _make_node(tmp_path, full_ledger=False)
    _make_blockstore(node)
    _make_ledger(node.hyper_path)
    chain_ops.guard_lost_ledger(node)   # ledger.db is cloned from hyper.db in this mode


def test_fresh_install_without_blockstore_still_bootstraps(tmp_path):
    node = _make_node(tmp_path)
    chain_ops.guard_lost_ledger(node)   # no store -> normal bootstrap path stays available


def test_empty_blockstore_does_not_block(tmp_path):
    node = _make_node(tmp_path)
    _make_blockstore(node, heights=())
    chain_ops.guard_lost_ledger(node)


@pytest.mark.parametrize("version", ["testnet", "regnet"])
def test_testnet_and_regnet_are_never_guarded(tmp_path, version):
    node = _make_node(tmp_path, version=version)
    _make_blockstore(node)
    chain_ops.guard_lost_ledger(node)
