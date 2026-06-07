"""
Mempool anti-spam admission tests (Mempool.space_left_for_tx).

Locks in the doc/17 rework: under congestion the pool admits by the tx's DETERMINISTIC FEE, not by
nominal amount (un-gameable) and not by a per-address allow-list (Sybil-trivial). space_left_for_tx
no longer touches self, so we call it unbound with a dummy self — no DB / node / network needed.

Run with: python3 -m pytest tests/test_mempool_antispam.py -v
"""
import types

from mempool import Mempool

_SELF = types.SimpleNamespace()


def _tx(amount=0, operation="", openfield=""):
    # transaction tuple positions read by space_left_for_tx: [3]=amount, [6]=operation, [7]=openfield
    t = [0] * 8
    t[1] = "sender_address"
    t[3] = amount
    t[6] = operation
    t[7] = openfield
    return t


def _left(tx, size):
    return Mempool.space_left_for_tx(_SELF, tx, size)


def test_empty_pool_accepts_anything():
    assert _left(_tx(amount=0), 0.1) is True
    assert _left(_tx(amount=0), 0.29) is True


def test_large_amount_does_not_buy_priority():
    # A huge (self-)send used to be admitted by the amount>5 tier for the price of one base fee.
    # That tier is gone: a bare payment pays only the base fee, so it is refused once the pool fills.
    assert _left(_tx(amount=1_000_000), 0.45) is False
    assert _left(_tx(amount=1_000_000), 0.35) is False  # also refused in the data tier (no fee premium)


def test_fee_buys_priority():
    # alias-register pays +1 BIS fee -> admitted in the 0.4-0.5 band
    assert _left(_tx(openfield="alias=myname"), 0.45) is True
    # token:issue pays +10 BIS fee -> admitted even in the tightest 0.5-0.6 band
    assert _left(_tx(operation="token:issue"), 0.55) is True


def test_data_tx_admitted_just_above_base():
    # any openfield content pushes the fee above base -> admitted in the 0.3-0.4 band
    assert _left(_tx(openfield="x"), 0.35) is True
    # but a bare payment (exactly base fee) is not
    assert _left(_tx(amount=3, openfield=""), 0.35) is False


def test_full_pool_rejects_all():
    assert _left(_tx(operation="token:issue"), 0.6) is False
    assert _left(_tx(operation="token:issue"), 0.7) is False
