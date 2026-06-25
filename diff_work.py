"""hf2 Stage-4: deterministic integer work units for one block (doc/40 — difficulty domain, D.2.1).

Bismuth difficulty is a log2 work domain: work(block) ~ 2**(difficulty/2). Computing that in float forks
nodes (libm last-bit divergence — the exact hazard difficulty_lwma.py calls out for math.log2 vs Decimal).
So work is defined ONLY in the Decimal domain under a fixed local precision, reusing the difficulty module's
deterministic discipline, and floored to a canonical integer. Two nodes computing work_units for the same
difficulty_e10 get the identical integer.

cumulative_work = running Σ work_units over [genesis..height] — an integer (no float drift across the chain),
stored as u128 (it is unbounded-growing; see diff_store).
"""
from decimal import Decimal, localcontext

__version__ = "0.0.1"

_WORK_PREC = 80          # >> the ~13 significant digits the result needs; isolates from caller context
_E10 = 10 ** 10          # difficulty is quantized to 10 dp (quantizer.quantize_ten); the integer grid


def work_units(difficulty_e10):
    """Deterministic integer work for one block. ``difficulty_e10`` = difficulty * 10**10 (the quantize_ten
    grid). Returns floor(2 ** (difficulty/2)) computed entirely in Decimal under a pinned precision."""
    difficulty_e10 = int(difficulty_e10)
    if difficulty_e10 <= 0:
        return 0
    with localcontext() as ctx:
        ctx.prec = _WORK_PREC
        d = Decimal(difficulty_e10) / Decimal(_E10)              # exact: integer / 10**10
        w = (Decimal(2).ln() * d / Decimal(2)).exp()            # 2**(d/2) = exp((d/2)*ln2), correctly rounded
        return int(w.to_integral_value(rounding="ROUND_FLOOR"))  # floor -> canonical deterministic integer
