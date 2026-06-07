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

Pure + unit-tested (tests/test_difficulty_lwma.py); NOT wired into consensus — it activates only behind
the fork gate (``block_height >= fork_height``), so adding it changes nothing today.
"""
import math

TARGET_BLOCK_TIME = 60      # seconds
WINDOW = 60                 # blocks averaged (recency-weighted)
MAX_STEP = 1.0              # symmetric per-retarget bound on the diff change (safety; rarely hit)
SOLVETIME_CLAMP = 6         # clamp each solvetime to [1, CLAMP*target] (anti-timestamp-manipulation)
MIN_DIFFICULTY = 50


def lwma_next_difficulty(solvetimes, prev_diff,
                         target=TARGET_BLOCK_TIME, window=WINDOW,
                         max_step=MAX_STEP, clamp=SOLVETIME_CLAMP, min_diff=MIN_DIFFICULTY):
    """Next difficulty from recent inter-block solvetimes (seconds), oldest..newest.

    Recency-weighted (linear) average solvetime, each clamped to ``[1, clamp*target]``; then a
    symmetric log-domain nudge ``2*log2(target/avg)`` toward the target, bounded to ``±max_step``.
    """
    st = list(solvetimes)[-window:]
    if not st:
        return float(prev_diff)
    num = den = 0.0
    for i, t in enumerate(st, start=1):                  # weight i: newest highest
        t = min(max(float(t), 1.0), clamp * target)      # clamp -> resist timestamp games
        num += t * i
        den += i
    avg_solvetime = num / den
    adjustment = 2.0 * math.log2(target / avg_solvetime)  # symmetric: slow->down, fast->up
    adjustment = max(-max_step, min(max_step, adjustment))
    return max(float(min_diff), float(prev_diff) + adjustment)
