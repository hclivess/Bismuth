"""
Block-store INTEGRATION on regnet.

With block_store=True (tests/config_custom.txt) the digester shadow-writes every committed block into
the LMDB store. This reads that live store — concurrently with the running node — and asserts it
mirrors the chain: same tip, and the same block_hash + tx-count for every block. That the whole suite
still passes with the store enabled is itself the proof the shadow write is additive (no effect on the
block hash / validation / mining).

Run with: python3 -m pytest tests/test_block_store_integration.py -v
"""
import sqlite3
import time

import pytest

pytest.importorskip("lmdb")

import block_store

STORE = "static/blockstore"


def test_store_mirrors_the_chain(client):
    client.mine(4)
    time.sleep(0.4)
    conn = sqlite3.connect("file:static/regmode.db?mode=ro", uri=True, timeout=20)
    conn.text_factory = str
    bs = block_store.BlockStore(STORE, readonly=True)
    try:
        tip = conn.execute("SELECT max(block_height) FROM transactions WHERE block_height>0").fetchone()[0]
        assert tip and tip > 1
        # the shadow write lands just after the SQL commit; give it a moment to catch up
        deadline = time.time() + 10
        while (bs.tip() or 0) < tip and time.time() < deadline:
            time.sleep(0.2)
        assert bs.tip() == tip, "store tip %s != ledger tip %s" % (bs.tip(), tip)

        # every digested block (genesis/block 1 is created at init, not shadow-written) matches
        for h in range(2, tip + 1):
            block_hash, count = conn.execute(
                "SELECT block_hash, count(*) FROM transactions WHERE block_height=?", (h,)).fetchone()
            assert bs.block_hash(h) == block_hash, "block %d hash mismatch" % h
            assert len(bs.get_block(h)) == count, "block %d tx-count mismatch" % h
    finally:
        bs.close()
        conn.close()
