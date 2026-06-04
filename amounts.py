"""
Canonical conversion between Bismuth's legacy decimal-string amounts and integer atomic units
(1 BIS = 100_000_000 units, i.e. 8 decimals). Foundation for the storage modernization (doc/16, phase 2).

Consensus serialization (transaction signing / block hashing) stays on the legacy ``'%.8f'`` string
form, frozen in ``bismuth_serialize.py``. This module is the boundary the *storage* layer will use to
keep amounts as integers internally while still reproducing the exact legacy strings at the consensus
and legacy-API edges. Integer arithmetic is exact (no float/Decimal rounding drift), which is also
what a future incremental balance index needs to bit-match the authoritative computation.
"""
from decimal import Decimal

from quantizer import quantize_eight

DECIMALS = 8
SATOSHIS_PER_BIS = 10 ** DECIMALS  # 100_000_000

# Set once at node startup from config.ledger_integer_amounts. When True, the ledger stores
# amount/fee/reward as integer atomic units (doc/16 phase 2 cutover); default False = legacy decimals.
LEDGER_INTEGER = False


def to_units(amount) -> int:
    """Legacy amount (str / Decimal / float / int) -> integer atomic units, rounded to 8 dp."""
    return int((quantize_eight(amount) * SATOSHIS_PER_BIS).to_integral_value())


def from_units(units: int) -> str:
    """Integer atomic units -> the legacy fixed-8-decimals string (e.g. 100000000 -> '1.00000000').

    Uses exact integer formatting (no float), so values like 99999999.99999999 round-trip precisely.
    """
    units = int(units)
    sign = "-" if units < 0 else ""
    units = abs(units)
    return "{}{}.{:08d}".format(sign, units // SATOSHIS_PER_BIS, units % SATOSHIS_PER_BIS)


def to_decimal(units) -> Decimal:
    """Integer atomic units -> Decimal BIS (8 dp)."""
    return quantize_eight(Decimal(int(units or 0)) / SATOSHIS_PER_BIS)


def ledger_value(raw) -> Decimal:
    """A ledger amount/fee/reward column value -> Decimal BIS, honoring the current storage mode.

    Use this wherever a ledger amount column (or a SUM of them) is read into a Decimal: it returns
    `to_decimal(raw)` when the ledger holds integer units, else the legacy `quantize_eight(raw)`.
    """
    if LEDGER_INTEGER:
        return to_decimal(raw)
    return quantize_eight(raw)
