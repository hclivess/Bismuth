"""
Post-fork dynamic base fee (fee_dynamics + block_store.recent_block_weights) — unit tests.

Congestion-responsive, smooth, clamped, deterministic: the fee rises under load and falls when idle, but
can't spike past the clamps, and is a pure function of recent block WEIGHT (tx count + openfield bytes //
W_UNIT) — so large RingCT/VM txs price in their real footprint, not just their count. The weight is read
from the post-fork BLOCK STORE (LMDB), never SQLite.

Run with: python3 -m pytest tests/test_fee_dynamics.py -v
"""
import os
import tempfile
from decimal import Decimal

import pytest

import fee_dynamics as fd

BASE = Decimal("0.01000000")


def test_at_target_is_roughly_static():
    assert fd.base_fee(BASE, [fd.TARGET_WEIGHT] * 5) == BASE
    assert fd.base_fee(BASE, []) == BASE                 # no data -> static


def test_responds_to_demand():
    high = fd.base_fee(BASE, [fd.TARGET_WEIGHT * 3] * 5)
    low = fd.base_fee(BASE, [1] * 5)
    assert high > BASE > low                              # busy chain dearer, idle chain cheaper


def test_clamped_no_runaway():
    assert fd.base_fee(BASE, [fd.TARGET_WEIGHT * 1000] * 5) == BASE * fd.MAX_MULT   # ceiling
    assert fd.base_fee(BASE, [0] * 5) == BASE * fd.MIN_MULT                          # floor


def test_deterministic():
    loads = [10, 50, 30, 5, 60, 12]
    assert fd.base_fee(BASE, loads) == fd.base_fee(BASE, loads)


def test_weight_prices_in_size_not_just_count():
    """The whole point of weight: the SAME number of txs costs MORE when the txs are large (RingCT/VM)
    than when they are tiny transfers — count alone can't see that."""
    tiny = [10] * 5                                       # 10 tiny txs/block -> weight ~10
    heavy = [10 + 5 * 4] * 5                              # 10 txs but each ~4 KB openfield -> +20 weight
    assert fd.base_fee(BASE, heavy) > fd.base_fee(BASE, tiny)


def _row(openfield):
    """A 12-field ledger row (height, ts, addr, recipient, amount, sig, pubkey, blockhash, fee, reward,
    operation, openfield); only the LAST field (openfield) affects the weight."""
    return ["1", "t", "a" * 56, "b" * 56, "0", "s", "pub", "h", "0", "0", "", openfield]


def test_recent_block_weights_from_block_store():
    """The congestion signal is read from the post-fork BLOCK STORE (LMDB), not SQLite."""
    pytest.importorskip("lmdb")
    import block_store
    bs = block_store.BlockStore(os.path.join(tempfile.mkdtemp(), "bstest"))
    try:
        # NB heights start at 2: block 1 (genesis) is created at ledger init and never enters the store,
        # so the window floors at 2 (block_store.recent_block_weights) and a height-1 entry is never read.
        bs.put_block(2, "h2", [_row(""), _row(""), _row("")])       # 3 tiny -> 3 + 0//1000   = 3
        bs.put_block(3, "h3", [_row("x" * 4000), _row("")])         # 2 txs  -> 2 + 4000//1000 = 6
        bs.put_block(4, "h4", [_row("y" * 100000)])                 # 1 big  -> 1 + 100000//1000 = 101
        weights = bs.recent_block_weights(4, window=20, w_unit=fd.W_UNIT)
        assert weights == [3, 6, 101], weights
        # window caps the lookback; a tip cut drops the rolled-off block (store is rolled back with chain)
        assert bs.recent_block_weights(3, window=20, w_unit=fd.W_UNIT) == [3, 6]
        # genesis is never part of the window, even when the lookback would reach it
        bs.put_block(1, "h1", [_row("")])
        assert bs.recent_block_weights(4, window=20, w_unit=fd.W_UNIT) == [3, 6, 101]
        assert fd.base_fee(BASE, weights) > BASE                    # avg(3,6,101)=36.7 > TARGET_WEIGHT 30
    finally:
        bs.close()
