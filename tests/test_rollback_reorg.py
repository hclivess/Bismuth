"""
End-to-end reorg test.

A real chain rollback must roll back the ledger AND every integrated auxiliary store (currently the
LMDB block store) **in sync**, through the single `chain_ops._rollback_aux_stores` path. Driven by the
regnet-only `regtest_rollback` command (a rollback command must never be reachable on a live chain).

This closes the gap the live outage exposed: nothing previously drove a node through a reorg and
asserted all stores stayed consistent.

Run with: python3 -m pytest tests/test_rollback_reorg.py -v
"""
import sqlite3
from time import sleep

import pytest

pytest.importorskip("lmdb")

import block_store

STORE = "static/blockstore-regmode.db"
LEDGER = "file:static/regmode.db?mode=ro"


def _ledger_tip():
    conn = sqlite3.connect(LEDGER, uri=True, timeout=20)
    conn.text_factory = str
    try:
        return conn.execute("SELECT max(block_height) FROM transactions WHERE block_height>0").fetchone()[0]
    finally:
        conn.close()


def test_reorg_rolls_back_ledger_and_stores_in_sync(client):
    client.mine(8)
    tip = client.block_height()
    assert tip == _ledger_tip()                       # node + ledger agree before
    target = tip - 4                                  # roll back BELOW target -> new tip = target-1
    new_tip = client.rollback(target)

    assert new_tip == target - 1                       # node height rolled back
    assert _ledger_tip() == target - 1                 # ledger rolled back

    bs = block_store.BlockStore(STORE, readonly=True)
    try:
        assert bs.tip() == target - 1                  # block store rolled back IN SYNC with the ledger
        conn = sqlite3.connect(LEDGER, uri=True, timeout=20)
        conn.text_factory = str
        try:
            lhash = conn.execute("SELECT block_hash FROM transactions WHERE block_height=? AND reward!=0",
                                 (target - 1,)).fetchone()[0]
            assert bs.block_hash(target - 1) == lhash  # store still matches the ledger at the new tip
            assert bs.get_block(target) is None        # rolled-off blocks gone from the store
            assert conn.execute("SELECT count(*) FROM transactions WHERE block_height>=?",
                                (target,)).fetchone()[0] == 0   # ...and from the ledger
        finally:
            conn.close()
    finally:
        bs.close()

    # mine forward again -> ledger and store advance together, no orphan/desync
    client.mine(2)
    sleep(0.3)
    assert client.block_height() == target + 1
    bs2 = block_store.BlockStore(STORE, readonly=True)
    try:
        assert bs2.tip() == target + 1
    finally:
        bs2.close()
