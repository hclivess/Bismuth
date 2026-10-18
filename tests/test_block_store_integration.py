"""
LMDB ledger INTEGRATION on regnet.

The running node's ledger is ONE LMDB environment (static/blockstore-regmode.db). This opens it read-only
— concurrently with the running node — and asserts the reader sees the node's chain and that the store is
internally consistent: same tip as the node reports, every block has a hash that maps back to its height
through the hash index, a coinbase, and the tx counter equals the rows actually stored.

Run with: python3 -m pytest tests/test_block_store_integration.py -v
"""
import time

import block_store

STORE = "static/blockstore-regmode.db"


def test_live_ledger_is_consistent_for_a_concurrent_reader(client):
    client.mine(4)
    time.sleep(0.6)
    tip = client.block_height()
    assert tip and tip > 1
    bs = block_store.BlockStore(STORE, readonly=True)
    try:
        deadline = time.time() + 10                  # the commit is atomic; allow for the tip moving on
        while (bs.tip() or 0) < tip and time.time() < deadline:
            time.sleep(0.2)
        assert bs.tip() >= tip, "reader tip %s < node tip %s" % (bs.tip(), tip)
        assert bs.first() == 1                        # genesis is in the ledger
        ntx = 0
        for h, rows in bs.blocks_in_range(1, tip):
            bh = bs.block_hash(h)
            assert bh and bs.height_by_hash(bh) == h, "block %d hash index mismatch" % h
            assert rows and all(r[block_store.H] == h for r in rows)
            if h > 1:
                assert bs.coinbase(h) is not None, "block %d has no coinbase" % h
                assert bs.difficulty(h) is not None, "block %d has no recorded difficulty" % h
            ntx += len(rows)
        if bs.tip() == tip:
            assert bs.tx_count() == ntx
    finally:
        bs.close()
