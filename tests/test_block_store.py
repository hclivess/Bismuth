"""
LMDB block-body store tests (block_store.BlockStore) — pure unit, no node/network.

Proves: lossless 12-field round-trip, NUMERIC height ordering (big-endian keys), hash->height index,
height-based rollback (including its hash-index cleanup), and that a store built from a SQLite ledger
is a byte-for-byte mirror (verify_against_sqlite) — and that the verifier actually catches a mismatch.

Run with: python3 -m pytest tests/test_block_store.py -v
"""
import sqlite3

import pytest

pytest.importorskip("lmdb")  # LMDB block store is an optional phase-7 dependency

import block_store
from block_store import BlockStore

SMALL = 64 * 1024 * 1024  # 64 MiB map is plenty for tests


def _row(h, i, bh):
    # a 12-field ledger row: block_height, timestamp, address, recipient, amount, signature,
    # public_key, block_hash, fee, reward, operation, openfield
    return [h, "%.2f" % (1600000000 + h), "addr%d" % i, "recip%d" % i, 0.5 + i,
            "sig_%d_%d" % (h, i), "pubkey%d" % i, bh, 0.01, 1.0 if i == 0 else 0,
            "op%d" % i, "openfield_%d_%d" % (h, i)]


def _block(h, ntx=2):
    bh = "hash%08d" % h
    return h, bh, [_row(h, i, bh) for i in range(ntx)]


def test_put_get_lossless_roundtrip(tmp_path):
    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        h, bh, rows = _block(5, ntx=3)
        s.put_block(h, bh, rows)
        assert s.get_block(5) == rows         # exact 12-field round-trip
        assert s.block_hash(5) == bh
        assert s.height_by_hash(bh) == 5
        assert s.get_block(99) is None
        assert s.height_by_hash("nope") is None
    finally:
        s.close()


def test_numeric_ordering_tip_count_range(tmp_path):
    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        s.put_blocks([_block(h) for h in (1, 2, 3, 10, 11)])
        assert s.tip() == 11
        assert s.count() == 5
        assert [h for h, _ in s.blocks_in_range(2, 10)] == [2, 3, 10]
        # add out-of-order; ordering must stay NUMERIC (would be 10<2 if keys were decimal strings)
        s.put_blocks([_block(100), _block(9)])
        assert s.tip() == 100
        assert [h for h, _ in s.blocks_in_range(9, 100)] == [9, 10, 11, 100]
    finally:
        s.close()


def test_rollback_removes_blocks_and_hashes(tmp_path):
    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        s.put_blocks([_block(h) for h in range(1, 11)])
        assert s.tip() == 10 and s.count() == 10
        assert s.rollback(6) == 4                 # 7..10 removed
        assert s.tip() == 6 and s.count() == 6
        assert s.get_block(7) is None
        assert s.get_block(6) is not None
        assert s.height_by_hash("hash%08d" % 7) is None   # hash index cleaned up
        assert s.height_by_hash("hash%08d" % 6) == 6
    finally:
        s.close()


def test_build_and_verify_against_sqlite(tmp_path):
    ledger = str(tmp_path / "ledger.db")
    conn = sqlite3.connect(ledger)
    conn.execute("CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, "
                 "amount, signature, public_key, block_hash, fee, reward, operation, openfield)")
    for h in range(1, 21):
        _, _, rows = _block(h, ntx=(h % 3) + 1)   # 1..3 txs per block
        conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        assert block_store.build_from_sqlite(ledger, s, start=1) == 20
        blocks, txs = block_store.verify_against_sqlite(ledger, s, start=1)
        assert blocks == 20 and txs > 20

        # corrupt one block in the store; the verifier must catch it
        s.put_block(10, "hash00000010", [_row(10, 0, "hash00000010")])  # wrong tx count
        try:
            block_store.verify_against_sqlite(ledger, s, start=1)
            assert False, "verify_against_sqlite failed to catch a mismatch"
        except AssertionError as e:
            assert "block 10" in str(e)
    finally:
        s.close()
