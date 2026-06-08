"""
Post-fork dynamic base fee (fee_dynamics) — unit tests.

Demand-responsive, smooth, clamped, deterministic: the fee rises under load and falls when idle, but
can't spike past the clamps, and is a pure function of the recent per-block tx counts.

Run with: python3 -m pytest tests/test_fee_dynamics.py -v
"""
from decimal import Decimal

import fee_dynamics as fd

BASE = Decimal("0.01000000")


def test_at_target_is_roughly_static():
    assert fd.base_fee(BASE, [fd.TARGET_TXS] * 5) == BASE
    assert fd.base_fee(BASE, []) == BASE                 # no data -> static


def test_responds_to_demand():
    high = fd.base_fee(BASE, [fd.TARGET_TXS * 3] * 5)
    low = fd.base_fee(BASE, [1] * 5)
    assert high > BASE > low                              # busy chain dearer, idle chain cheaper


def test_clamped_no_runaway():
    assert fd.base_fee(BASE, [fd.TARGET_TXS * 1000] * 5) == BASE * fd.MAX_MULT   # ceiling
    assert fd.base_fee(BASE, [0] * 5) == BASE * fd.MIN_MULT                       # floor


def test_deterministic():
    counts = [10, 50, 30, 5, 60, 12]
    assert fd.base_fee(BASE, counts) == fd.base_fee(BASE, counts)
