"""
block_store helpers — column typing, key encodings, balance packing, unwrapping. Pure, no store.

Run with: python3 -m pytest tests/test_ledger_helpers.py -v
"""
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import amounts  # noqa: E402
import block_store  # noqa: E402
from block_store import numeric, real_text, text  # noqa: E402


@pytest.mark.parametrize("v,expected", [
    ("7", 7), (" -3 ", -3), ("+12", 12),
    ("1.25", 1.25), ("3.0", 3), ("0E-8", 0), (".5", 0.5), ("1e3", 1000), ("2.5e-1", 0.25),
    ("abc", "abc"), ("", ""), ("1.2.3", "1.2.3"), ("0x10", "0x10"),
    (True, 1), (False, 0), (Decimal("4.00000000"), 4), (Decimal("0.10000000"), 0.1),
    (2.0, 2), (2.5, 2.5), (None, None),
])
def test_numeric_affinity(v, expected):
    got = numeric(v)
    assert got == expected and type(got) is type(expected)


def test_numeric_out_of_i64_range_becomes_float():
    big = str(1 << 63)
    assert isinstance(numeric(big), float)
    assert numeric(str(-(1 << 63))) == -(1 << 63)          # the lower bound is still an int
    assert isinstance(numeric(str(-(1 << 63))), int)


@pytest.mark.parametrize("f,expected", [
    (1.0, "1.0"), (50.0, "50.0"), (88.8928995217, "88.8928995217"), (0.1, "0.1"),
    (1e20, "1e+20"), (float("inf"), "inf"), (123456789012345678.0, "1.23456789012346e+17"),
])
def test_real_text_matches_real_to_text(f, expected):
    assert real_text(f) == expected


def test_text_affinity():
    assert text("x") == "x"
    assert text(52.0) == "52.0"
    assert text(52) == "52"
    assert text(b"\xffab") == "�ab"
    assert text(memoryview(b"pk")) == "pk"


def test_typed_row_is_a_tuple_with_ledger_types():
    raw = ["5", "1600000000.25", "a", "b", "0.50000000", "sig", "pk", "h", "0.01", "0", "op", "123"]
    r = block_store.typed_row(raw)
    assert isinstance(r, tuple)
    assert r == (5, 1600000000.25, "a", "b", 0.5, "sig", "pk", "h", 0.01, 0, "op", "123")
    assert type(r[11]) is str            # openfield is TEXT even when it looks numeric


def test_units_decimal_and_integer_modes():
    assert block_store.units(None) == 0 and block_store.units("") == 0
    assert block_store.units("1.5") == 150000000
    assert block_store.units(0.00000001) == 1
    amounts.LEDGER_INTEGER = True
    assert block_store.units("150000000") == 150000000
    assert block_store.units(7) == 7


def test_is_coinbase_row():
    row = lambda reward: (1, 1, "a", "a", 0, "s", "p", "h", 0, reward, "0", "")  # noqa: E731
    assert block_store.is_coinbase_row(row(10))
    assert block_store.is_coinbase_row(row("0.5"))
    assert not block_store.is_coinbase_row(row(0))
    assert not block_store.is_coinbase_row(row("0"))
    assert not block_store.is_coinbase_row(row("junk"))
    assert not block_store.is_coinbase_row(row(None))


def test_height_keys_sort_numerically_including_negatives():
    hs = [-5, -1, 0, 1, 2, 10, 255, 256, 1 << 40]
    keys = [block_store._sh(h) for h in hs]
    assert sorted(keys) == keys
    assert [block_store._unsh(k) for k in keys] == hs


def test_address_keys_group_by_address_then_height():
    a, b = "a" * 56, "ab"
    ks = [block_store._addr_key(0, a, 9, 0), block_store._addr_key(0, a, 10, 0), block_store._addr_key(0, a, 10, 3)]
    assert sorted(ks) == ks
    # the length byte keeps a short address's keys out of a longer address's prefix range
    assert not block_store._addr_key(0, "a", 1, 0).startswith(block_store._addr_prefix(0, "ab"))
    assert block_store._addr_key(1, b, 1, 0)[0] == 1
    assert block_store._split_pos(block_store._addr_key(1, b, 1234, 7)) == (1234, 7)


def test_sig_key_truncates_to_legacy_txid_and_keeps_position():
    long_sig = "S" * 300
    k = block_store._sig_key(long_sig, 42, 3)
    assert k.startswith(b"S" * block_store.SIG_KEY_LEN + b"\x00")
    assert block_store._split_pos(k) == (42, 3)
    assert not block_store._indexable_sig("") and not block_store._indexable_sig("0")
    assert not block_store._indexable_sig(None) and block_store._indexable_sig("abc")


def test_balance_pack_roundtrip_signed_i128():
    v = [0, 1, -1, (1 << 120) - 1]
    b = block_store._bal_pack(v)
    assert len(b) == 64
    assert block_store._bal_unpack(b) == v


def test_fold_credits_recipient_debits_sender():
    acc = {}
    block_store._fold(acc, (2, 1, "s", "r", "2", "x", "p", "h", "0.01", "0", "0", ""))
    block_store._fold(acc, (2, 1, "m", "m", "0", "y", "p", "h", "0", "10", "0", ""))
    assert acc["r"] == [200000000, 0, 0, 0]
    assert acc["s"] == [0, 0, 200000000, 1000000]
    assert acc["m"] == [0, 1000000000, 0, 0]
    block_store._fold(acc, (2, 1, "s", "r", "2", "x", "p", "h", "0.01", "0", "0", ""), sign=-1)
    assert acc["r"] == [0, 0, 0, 0] and acc["s"] == [0, 0, 0, 0]


def test_as_ledger_unwraps_handlers_but_not_the_kvstore(tmp_path):
    s = block_store.BlockStore(str(tmp_path / "l"), map_size=1 << 24)
    try:
        class Handler:
            store = s
        assert block_store.as_ledger(s) is s
        assert block_store.as_ledger(Handler()) is s
        # the regression: BlockStore.store is the raw KVStore — as_ledger must NOT descend into it
        assert block_store.as_ledger(s) is not s.store
        assert block_store.as_ledger(None) is None
    finally:
        s.close()
