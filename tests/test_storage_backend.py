"""
Node-free tests for the storage read seam (``storage_backend``, doc/26 stage 3): the SqliteBackend
(legacy ledger.db) and the LmdbBackend (block_store) must return BYTE-IDENTICAL block/tx data. This is the
cross-check that lets a read be migrated from SQLite to LMDB safely.
"""
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("lmdb")
import block_store
import storage_backend


def _make_ledger(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, amount, "
                 "signature, public_key, block_hash, fee, reward, operation, openfield)")
    rows = []
    # block 1: a coinbase + a normal tx; block 2: one tx. (block_hash is column index 7.)
    rows.append((1, 100.0, "miner", "miner", "10", "sigA", "pkX", "hash1", "0", "1", "0", ""))
    rows.append((1, 100.1, "alice", "bob", "5", "sigB", "pkX", "hash1", "0.01", "0", "token:transfer", "tokx:5"))
    rows.append((2, 101.0, "miner2", "miner2", "10", "sigC", "pkY", "hash2", "0", "1", "0", ""))
    conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit(); conn.close()


def test_backends_agree(tmp_path):
    ledger = str(tmp_path / "ledger.db")
    _make_ledger(ledger)

    store = block_store.BlockStore(str(tmp_path / "bs"), map_size=64 * 1024 * 1024)
    n = block_store.build_from_sqlite(ledger, store, start=1)
    assert n == 2

    sq = storage_backend.SqliteBackend(ledger)
    lm = storage_backend.LmdbBackend(store)

    # tips + per-block reads match
    assert sq.tip_height() == lm.tip_height() == 2
    assert sq.get_block(1) == lm.get_block(1)
    assert sq.get_block(2) == lm.get_block(2)
    assert sq.get_block(1)[0][7] == "hash1"           # block_hash column
    assert sq.block_hash(2) == lm.block_hash(2) == "hash2"
    assert sq.height_by_hash("hash1") == lm.height_by_hash("hash1") == 1
    assert lm.get_block(99) is None and sq.get_block(99) is None

    # the cross-check the migration relies on
    blocks, txs = storage_backend.cross_check(sq, lm, 1, 2)
    assert (blocks, txs) == (2, 3)

    sq.close(); store.close()


def test_select_prefork_is_sqlite(tmp_path):
    ledger = str(tmp_path / "ledger.db")
    _make_ledger(ledger)

    class _Node:
        def __init__(self):
            self.ledger_path = ledger
            self.fork_height = None        # pre-fork
            self.last_block = 5
            self.block_store = None
    be = storage_backend.select(_Node())
    assert isinstance(be, storage_backend.SqliteBackend)
    assert be.tip_height() == 2
    be.close()


def test_select_postfork_with_store_is_lmdb(tmp_path):
    ledger = str(tmp_path / "ledger.db")
    _make_ledger(ledger)
    store = block_store.BlockStore(str(tmp_path / "bs"), map_size=64 * 1024 * 1024)
    block_store.build_from_sqlite(ledger, store, start=1)

    class _Node:
        def __init__(self):
            self.ledger_path = ledger
            self.fork_height = 1           # post-fork
            self.last_block = 5
            self.block_store = store
    be = storage_backend.select(_Node())
    assert isinstance(be, storage_backend.LmdbBackend)
    assert be.get_block(2)[0][7] == "hash2"
    store.close()


def test_write_backend_append_rollback_and_crosscheck(tmp_path):
    # The stage-4 write seam: append blocks atomically to the LMDB store, read them back, roll back a reorg,
    # and prove the written rows match the SQLite reference (the trust check that gates the canonical flip).
    ledger = str(tmp_path / "ledger.db")
    _make_ledger(ledger)                                   # the SQLite reference (blocks 1, 2)

    store = block_store.BlockStore(str(tmp_path / "bs"), map_size=64 * 1024 * 1024)
    writer = storage_backend.LmdbWriteBackend(store)
    # write the same blocks through the write seam (rows exactly as the SQLite ledger holds them)
    ref = storage_backend.SqliteBackend(ledger)
    for height, rows in ref.blocks_in_range(1, 2):
        writer.append_block(height, rows[0][7], rows)
    assert writer.committed_tip() == 2

    # read back via the read seam + cross-check against SQLite — byte-identical
    lm = storage_backend.LmdbBackend(store)
    assert lm.get_block(1) == ref.get_block(1)
    blocks, txs = storage_backend.cross_check(ref, lm, 1, 2)
    assert (blocks, txs) == (2, 3)

    # reorg: roll back above height 1 — atomic, single store, no lockstep
    removed = writer.rollback(1)
    assert removed == 1 and writer.committed_tip() == 1
    assert lm.get_block(2) is None and lm.get_block(1) is not None

    ref.close(); store.close()
