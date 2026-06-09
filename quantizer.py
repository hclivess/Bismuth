"""Fixed-precision Decimal rounding helpers used throughout Bismuth.

Bismuth amounts are handled as :class:`decimal.Decimal` values rounded to a
fixed number of decimal places (8 for amounts/fees/rewards, 2 and 10 for a few
display/intermediate sites). These tiny helpers centralise that rounding so the
same precision is applied everywhere amounts are read or compared.

This module is widely imported (the money-math foundation), so its behaviour is
deliberately frozen:

  * Each ``quantize_*`` accepts anything ``Decimal(value)`` accepts (str / int /
    float / Decimal).
  * The leading ``if not value`` short-circuit returns the pre-built zero
    constant for *any* falsy input -- ``0``, ``0.0``, ``''``, ``None`` and
    ``False`` all map to the corresponding zero Decimal (see ``quantize_eight``).
  * Rounding uses Decimal's default ROUND_HALF_EVEN via ``.quantize``.
"""
from decimal import Decimal

DECIMAL_ZERO_2DP = Decimal('0.00')
DECIMAL_ZERO_8DP = Decimal('0.00000000')
DECIMAL_ZERO_10DP = Decimal('0.0000000000')


def quantize_two(value) -> Decimal:
    """Round ``value`` to 2 decimal places (falsy input -> ``0.00``)."""
    if not value:
        return DECIMAL_ZERO_2DP
    value = Decimal(value)
    value = value.quantize(DECIMAL_ZERO_2DP)
    return value


def quantize_eight(value) -> Decimal:
    """Round ``value`` to 8 decimal places (falsy input -> ``0.00000000``).

    This is the canonical amount precision: 1 BIS = 8 decimals.
    """
    if not value:
        # Will match 0 as well as False and None
        return DECIMAL_ZERO_8DP
    value = Decimal(value)
    value = value.quantize(DECIMAL_ZERO_8DP)
    return value


def quantize_ten(value) -> Decimal:
    """Round ``value`` to 10 decimal places (falsy input -> ``0.0000000000``)."""
    if not value:
        return DECIMAL_ZERO_10DP
    value = Decimal(value)
    value = value.quantize(DECIMAL_ZERO_10DP)
    return value
