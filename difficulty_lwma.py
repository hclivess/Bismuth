"""
LWMA difficulty retarget — the proposed hf2 replacement for difficulty.py (doc/18 §C).

WHY: the current controller estimates hashrate indirectly, adds a derivative kick
(``-KD_GAIN*(block_time-block_time_prev)``) that spikes on a burst of fast blocks, and **caps up-moves
but not down-moves** (``MAX_DIFF_ADJUST`` is up-only; the only way down is a slow 180s emergency drop).
Result: difficulty ratchets up harshly and then sticks HIGH when hashrate leaves — brutal steps and a
small-chain stall.

LWMA (Zawy's Linear Weighted Moving Average) fixes this: a recency-weighted average of recent
solvetimes (newest weighted most) drives a **symmetric**, bounded nudge toward the target block time.
Fast blocks raise difficulty, slow blocks lower it by the same law; a single manipulated timestamp is
clamped and barely moves the windowed average; there are no PID gains to hand-tune.

Bismuth's difficulty is a LOG2 *work* domain (work ~ ``2**(diff/2)``), so scaling work by a factor f
means adding ``2*log2(f)`` to diff. Equilibrium is solvetime == target → zero change.

DETERMINISM: this is a consensus path, so the whole retarget runs in the Decimal domain under a FIXED
local precision — never float. ``math.log2`` is libm, whose last-bit result can differ across platforms;
``Decimal.ln`` is correctly-rounded to the context precision (the decimal spec), so every node computes a
bit-identical value. The function returns a ``Decimal`` (the consensus caller passes it through
``quantize_ten``); call ``float(...)`` if a float is wanted.

Pure + unit-tested (tests/test_difficulty_lwma.py); NOT wired into consensus — it activates only behind
the fork gate (``block_height >= fork_height``), so adding it changes nothing today.
"""
from decimal import Decimal, localcontext

TARGET_BLOCK_TIME = 60      # seconds
WINDOW = 60                 # blocks averaged (recency-weighted)
MAX_STEP = 1.0              # symmetric per-retarget bound on the diff change (safety; rarely hit)
SOLVETIME_CLAMP = 6         # clamp each solvetime to [1, CLAMP*target] (anti-timestamp-manipulation)
MIN_DIFFICULTY = 50

# Fixed working precision for the retarget. 50 significant digits is far more than the ~10 dp the result
# is later quantized to (quantize_ten), so the correctly-rounded Decimal.ln dominates any rounding and the
# output is stable. localcontext() isolates this from whatever precision the caller's context happens to
# carry, so the computation is reproducible regardless of global decimal state.
_PRECISION = 50


def lwma_next_difficulty(solvetimes, prev_diff,
                         target=TARGET_BLOCK_TIME, window=WINDOW,
                         max_step=MAX_STEP, clamp=SOLVETIME_CLAMP, min_diff=MIN_DIFFICULTY):
    """Next difficulty (``Decimal``) from recent inter-block solvetimes (seconds), oldest..newest.

    Recency-weighted (linear) average solvetime, each clamped to ``[1, clamp*target]``; then a
    symmetric log-domain nudge ``2*log2(target/avg)`` toward the target, bounded to ``±max_step``.
    Computed entirely in Decimal under a fixed precision so it is bit-identical across platforms.
    """
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        st = list(solvetimes)[-window:]
        prev_diff_d = Decimal(str(prev_diff))
        if not st:
            return prev_diff_d
        target_d = Decimal(str(target))
        clamp_hi = Decimal(clamp) * target_d
        one = Decimal(1)
        num = Decimal(0)
        den = Decimal(0)
        for i, t in enumerate(st, start=1):                  # weight i: newest highest
            td = Decimal(str(t))
            if td < one:                                     # clamp -> resist timestamp games
                td = one
            elif td > clamp_hi:
                td = clamp_hi
            wi = Decimal(i)
            num += td * wi
            den += wi
        avg_solvetime = num / den
        # symmetric: slow->down, fast->up. log2(x) = ln(x)/ln(2); Decimal.ln is correctly rounded -> deterministic.
        adjustment = Decimal(2) * ((target_d / avg_solvetime).ln() / Decimal(2).ln())
        max_step_d = Decimal(str(max_step))
        if adjustment > max_step_d:
            adjustment = max_step_d
        elif adjustment < -max_step_d:
            adjustment = -max_step_d
        result = prev_diff_d + adjustment
        min_diff_d = Decimal(str(min_diff))
        return result if result > min_diff_d else min_diff_d
