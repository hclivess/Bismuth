"""Unit tests for the integer-atomic-unit amount converter (pure; no node needed)."""
# Unit tests for the integer-atomic-unit amount converter (pure; no node needed).
# Run with: python3 -m pytest -v

from decimal import Decimal

import amounts


def test_to_units():
    assert amounts.to_units("0.00000000") == 0
    assert amounts.to_units("0.00000001") == 1
    assert amounts.to_units("1.00000000") == 100_000_000
    assert amounts.to_units(Decimal("12.34567890")) == 1_234_567_890
    assert amounts.to_units(0) == 0
    assert amounts.to_units("1.5") == 150_000_000


def test_from_units():
    assert amounts.from_units(0) == "0.00000000"
    assert amounts.from_units(1) == "0.00000001"
    assert amounts.from_units(100_000_000) == "1.00000000"
    assert amounts.from_units(1_234_567_890) == "12.34567890"


def test_round_trip_exact():
    for s in ["0.00000000", "0.00000001", "1.00000000", "12.34567890",
              "5.37309091", "99999999.99999999"]:
        assert amounts.from_units(amounts.to_units(s)) == s


def test_from_units_matches_legacy_8f_for_8dp_values():
    # legacy on-the-wire form is '%.8f'; for already-8dp values it must match exactly
    for s in ["0.00000000", "1.00000000", "12.34567890", "5.37309091"]:
        assert amounts.from_units(amounts.to_units(s)) == "%.8f" % float(s)


def test_to_decimal():
    assert amounts.to_decimal(0) == Decimal("0.00000000")
    assert amounts.to_decimal(1) == Decimal("0.00000001")
    assert amounts.to_decimal(100_000_000) == Decimal("1.00000000")
    assert amounts.to_decimal(150_000_000) == Decimal("1.50000000")
    assert amounts.to_decimal(None) == Decimal("0.00000000")


def test_ledger_value_honors_mode():
    # legacy mode: pass a decimal value through (quantize_eight)
    amounts.LEDGER_INTEGER = False
    assert amounts.ledger_value("1.50000000") == Decimal("1.50000000")
    # integer mode: interpret the value as atomic units
    amounts.LEDGER_INTEGER = True
    try:
        assert amounts.ledger_value(150_000_000) == Decimal("1.50000000")
    finally:
        amounts.LEDGER_INTEGER = False  # restore so other in-process tests are unaffected
