"""
End-to-end reorg test.

A real chain rollback must roll back the LMDB ledger — blocks AND every index inside it (hash index,
difficulty, balances, signatures) — in ONE transaction (``chain_ops.rollback``). Driven by the regnet-only
`regtest_rollback` command (a rollback command must never be reachable on a live chain).

Run with: python3 -m pytest tests/test_rollback_reorg.py -v
"""
from time import sleep

import block_store

STORE = "static/blockstore-regmode.db"


def _ledger_tip():
    bs = block_store.BlockStore(STORE, readonly=True)
    try:
        return bs.tip()
    finally:
        bs.close()


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
        assert bs.tip() == target - 1                  # every index rolled back with the blocks
        assert bs.last_difficulty()[0] == target - 1   # difficulty rows of the rolled-off blocks are gone
        lhash = bs.block_hash(target - 1)
        assert lhash and bs.height_by_hash(lhash) == target - 1
        assert bs.get_block(target) is None            # rolled-off blocks gone
        assert list(bs.blocks_in_range(target, target + 10)) == []
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
