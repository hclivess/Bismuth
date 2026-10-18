"""
Fresh-ledger bootstrap tests (chain_ops.bootstrap, doc/43 DB-direct peer snapshot).

A fresh node either installs a DB-direct LMDB snapshot fetched from a peer (``bootstrap_p2p``) into the
ledger's own store path, or — with bootstrap_p2p off, ``sync_from_genesis`` set, no snapshot available, or
a failing fetch — returns False so the node seeds genesis and syncs. A failure is logged, never swallowed
silently, and never leaves a half-installed store. No node subprocess, no network.

Run with: python3 -m pytest tests/test_bootstrap_local.py -v
"""
import os
import types

import chain_ops
import kvstore
import snapshot_p2p


class _Log:
    def __init__(self):
        self.lines = []

    def warning(self, m):
        self.lines.append(str(m))

    info = error = warning


def _make_node(tmp_path, **kw):
    node = types.SimpleNamespace(ledger_path=os.path.join(str(tmp_path), "ledger.db"), bootstrap_p2p=True,
                                 sync_from_genesis=False, logger=types.SimpleNamespace(app_log=_Log()))
    for k, v in kw.items():
        setattr(node, k, v)
    return node


def test_bootstrap_installs_a_peer_snapshot(tmp_path, monkeypatch):
    node = _make_node(tmp_path)

    def fake_fetch(n, cands, dest_dir, **k):
        os.makedirs(dest_dir)
        open(os.path.join(dest_dir, "data.mdb"), "w").write("snapshot")
        return cands[0], {"height": 1234}
    monkeypatch.setattr(snapshot_p2p, "candidates", lambda n: [("127.0.0.1", 1)])
    monkeypatch.setattr(snapshot_p2p, "fetch_db_from_peers", fake_fetch)

    assert chain_ops.bootstrap(node) is True
    dest = kvstore.store_path(node.ledger_path, "blockstore")
    assert open(os.path.join(dest, "data.mdb")).read() == "snapshot"     # installed at the ledger's store path
    assert not os.path.exists(dest + ".snapshot")                        # the temp dir was moved, not copied
    assert any("height 1234" in line for line in node.logger.app_log.lines)


def test_bootstrap_is_off_without_p2p_or_with_sync_from_genesis(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(snapshot_p2p, "candidates", lambda n: called.append(1) or [])
    assert chain_ops.bootstrap(_make_node(tmp_path, bootstrap_p2p=False)) is False
    assert chain_ops.bootstrap(_make_node(tmp_path, sync_from_genesis=True)) is False
    assert called == []                                                   # never even looked for peers


def test_bootstrap_without_peers_falls_back_to_genesis(tmp_path, monkeypatch):
    node = _make_node(tmp_path)
    monkeypatch.setattr(snapshot_p2p, "candidates", lambda n: [])
    assert chain_ops.bootstrap(node) is False
    assert any("syncing from genesis" in line for line in node.logger.app_log.lines)


def test_bootstrap_failure_is_logged_and_installs_nothing(tmp_path, monkeypatch):
    node = _make_node(tmp_path)

    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(snapshot_p2p, "candidates", lambda n: [("127.0.0.1", 1)])
    monkeypatch.setattr(snapshot_p2p, "fetch_db_from_peers", boom)
    assert chain_ops.bootstrap(node) is False
    assert any("snapshot bootstrap failed" in line for line in node.logger.app_log.lines)
    assert not os.path.exists(kvstore.store_path(node.ledger_path, "blockstore"))
