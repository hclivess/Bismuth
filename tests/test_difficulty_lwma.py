"""
LWMA difficulty retarget (difficulty_lwma.lwma_next_difficulty) — pure unit tests.

Locks the properties the current controller FAILS: a stable chain doesn't move; the response is
SYMMETRIC (slow blocks lower difficulty by the same law that fast blocks raise it — the current algo
only ratchets up and sticks high); single timestamp spikes are absorbed; and a constant hashrate
converges to the target block time.

Run with: python3 -m pytest tests/test_difficulty_lwma.py -v
"""
import math

from difficulty_lwma import lwma_next_difficulty

T = 60


def test_stable_chain_holds_difficulty():
    nxt = lwma_next_difficulty([T] * 60, prev_diff=100.0, target=T)
    assert abs(nxt - 100.0) < 1e-9                      # at target -> no change


def test_fast_blocks_raise_difficulty():
    nxt = lwma_next_difficulty([T // 2] * 60, prev_diff=100.0, target=T)
    assert nxt > 100.0


def test_slow_blocks_LOWER_difficulty():
    # the key fix: slow blocks must bring difficulty DOWN (old algo got stuck high)
    nxt = lwma_next_difficulty([T * 2] * 60, prev_diff=100.0, target=T)
    assert nxt < 100.0


def test_response_is_symmetric():
    up = lwma_next_difficulty([T // 2] * 60, 100.0, target=T) - 100.0      # 2x fast
    down = 100.0 - lwma_next_difficulty([T * 2] * 60, 100.0, target=T)     # 2x slow
    assert abs(up - down) < 1e-9                        # same magnitude, opposite sign


def test_bounded_per_step():
    far = lwma_next_difficulty([T * 1000] * 60, 100.0, target=T, max_step=1.0)
    assert far >= 100.0 - 1.0 - 1e-9                    # can't crater in one retarget


def test_absorbs_a_single_timestamp_spike():
    # 59 normal blocks + one absurd solvetime -> clamp + windowing keep the move small (vs the ~11.8
    # unclamped log-nudge); even at the newest (max-weighted) slot it moves diff < 0.5
    series = [T] * 59 + [T * 100000]
    nxt = lwma_next_difficulty(series, 100.0, target=T)
    assert abs(nxt - 100.0) < 0.5


def test_delicacy_steps_are_fine_under_normal_variance():
    # the property the user cares about: under normal block-time jitter the difficulty drifts in
    # FINE increments (a predictable trajectory), never a brutal jump. +/-30% jitter averaging to
    # target -> the worst single-block step is a ~1% block-time nudge, far below the 1.0 hard cap.
    patt = [T * 0.7, T * 1.3, T * 0.85, T * 1.15]      # averages exactly to T
    D, series, worst = 100.0, [T] * 60, 0.0
    for k in range(300):
        series = (series + [patt[k % 4]])[-60:]
        nd = lwma_next_difficulty(series, D, target=T)
        worst = max(worst, abs(nd - D))
        D = nd
    assert worst < 0.05                                # < ~1.7% block-time change per block


def test_calculable_deterministically_for_all():
    # the next challenge is a PURE function of public chain data: every miner computes the SAME value
    # in advance, before mining. No surprise, no privileged knowledge -- one challenge for all.
    series = [55, 62, 58, 60, 59, 61] * 10
    assert lwma_next_difficulty(series, 100.0, target=T) == lwma_next_difficulty(list(series), 100.0, target=T)


def test_converges_to_target_on_average():
    # constant hashrate whose equilibrium difficulty is Dstar; the feedback solvetime depends on the
    # current diff. The retarget must keep difficulty PARKED near equilibrium and the AVERAGE block
    # time at target -- no ratchet, no stuck-high death spiral (it oscillates gently, centered on T).
    Dstar, D = 102.0, 100.0
    series = [T] * 60
    settled = []
    for it in range(400):
        solvetime = T * (2 ** ((D - Dstar) / 2))
        series = (series + [solvetime])[-60:]
        D = lwma_next_difficulty(series, D, target=T, max_step=1.0)
        if it >= 250:
            settled.append(solvetime)
    assert abs(D - Dstar) < 2.0                         # difficulty parks near equilibrium
    assert abs(sum(settled) / len(settled) - T) < T * 0.15   # average block time ~ target
