"""Proof-of-work difficulty retargeting.

Computes the network difficulty the next block must satisfy, from the recent
history of block solve times in the ledger. The retarget steers the average
block interval toward the target by raising difficulty when blocks arrive too
fast and lowering it when they arrive too slowly, with the exact formula
selected per fork era (via ``fork``) and special-cased for regnet. Because the
returned value is the threshold ``digest`` checks the proof of work against,
this calculation is consensus-critical and must be deterministic across nodes.
"""

import block_store
from decimal import Decimal
import regnet
import math
import time
from fork import *
from quantizer import quantize_two, quantize_eight, quantize_ten
from fork import Fork

# Difficulty controller constants (a PID-style controller targeting a fixed block time).
TARGET_BLOCK_TIME = Decimal(60.00)   # desired seconds per block
DIFFICULTY_WINDOW = 1440             # rolling window (blocks) used to average block time
KD_GAIN = 10                         # derivative gain of the feedback controller
DIFF_ADJUST_DIVISOR = 720            # damps the per-block difficulty change
MAX_DIFF_ADJUST = Decimal(1.0)       # cap on a single-block upward adjustment
DIFF_DROP_TIME = Decimal(180)        # seconds before the broadcast difficulty begins to drop
MIN_DIFFICULTY = 50                  # difficulty floor


def _ledger(db_handler):
    return block_store.as_ledger(db_handler)


def coinbase_ts(store, height):
    """The mining-tx timestamp of block ``height`` (typed: float/int, as the ledger column holds it)."""
    cb = store.coinbase(int(height))
    return None if cb is None else cb[1]


def _recent_solvetimes(db_handler, block_height, window):
    """The last ~window inter-block solvetimes (seconds), oldest->newest, from the miner-tx timestamps of
    the blocks above block_height - window - 1. Fed to the LWMA retarget."""
    store = _ledger(db_handler)
    first = store.first() or 1
    lo = max(int(first), int(block_height) - int(window))
    ts = []
    for h in range(lo, int(block_height) + 1):
        t = coinbase_ts(store, h)
        if t is not None:
            ts.append(float(t))
    return [ts[i] - ts[i - 1] for i in range(1, len(ts))]


def retarget(diff_block_previous, timestamp_last, timestamp_before_last, timestamp_1441, timestamp_1440):
    """The legacy PID retarget: the next required difficulty from the previous recorded difficulty and the
    four window timestamps (all Decimal). Returns (difficulty, hashrate, diff_adjustment, block_time).
    Pure — the live node and the history rebuild share it."""
    block_time_prev = (timestamp_before_last - timestamp_1441) / DIFFICULTY_WINDOW
    block_time = Decimal(timestamp_last - timestamp_1440) / DIFFICULTY_WINDOW
    hashrate = pow(2, diff_block_previous / Decimal(2.0)) / (
            block_time * math.ceil(28 - diff_block_previous / Decimal(16.0)))
    difficulty_new = Decimal(
        (2 / math.log(2)) * math.log(hashrate * TARGET_BLOCK_TIME * math.ceil(28 - diff_block_previous / Decimal(16.0))))
    difficulty_new = difficulty_new - KD_GAIN * (block_time - block_time_prev)
    diff_adjustment = (difficulty_new - diff_block_previous) / DIFF_ADJUST_DIVISOR
    if diff_adjustment > MAX_DIFF_ADJUST:
        diff_adjustment = MAX_DIFF_ADJUST
    return quantize_ten(diff_block_previous + diff_adjustment), hashrate, diff_adjustment, block_time


def window_timestamps(store, block_height, first=None):
    """(ts_last, ts_before_last, ts_1441, ts_1440) as Decimal for the retarget after block ``block_height``."""
    first = (store.first() or 1) if first is None else first
    ts_last = Decimal(coinbase_ts(store, block_height))
    prev = coinbase_ts(store, block_height - 1) if block_height - 1 >= first else None
    ts_before_last = ts_last if prev is None else Decimal(prev)
    # the first two mining txs above block_height - 1441 (clamped to the first stored block)
    lo = max(int(first), int(block_height) - DIFFICULTY_WINDOW)
    t1441 = coinbase_ts(store, lo)
    t1440 = coinbase_ts(store, lo + 1) if lo + 1 <= block_height else None
    ts_1441 = Decimal(t1441)
    ts_1440 = ts_1441 if t1440 is None else Decimal(t1440)
    return ts_last, ts_before_last, ts_1441, ts_1440


def recorded(difficulty):
    """The form a computed difficulty is recorded in (10 dp, as the PoW check stores it)."""
    return float('%.10f' % difficulty)


def difficulty(node, db_handler):
    try:
        fork = Fork()
        store = _ledger(db_handler)

        block_height = store.tip()
        timestamp_last, timestamp_before_last, timestamp_1441, timestamp_1440 = window_timestamps(store, block_height)

        node.last_block_timestamp = timestamp_last
        node.last_block_ago = int(time.time() - int(timestamp_last))

        _dh, _dtext = store.last_difficulty()
        diff_block_previous = Decimal(_dtext)

        time_to_generate = timestamp_last - timestamp_before_last
        block_time = Decimal(timestamp_last - timestamp_1440) / DIFFICULTY_WINDOW

        if node.is_regnet:
            # TEST-ONLY divergence injection for the #23 regnet soak (doc/35): BISMUTH_TEST_DIFF_OFFSET shifts
            # THIS node's reported regnet difficulty so a soak can make one node genuinely diverge from its
            # peers. Inert in prod — mainnet never takes the is_regnet branch — and 0 unless the env var is set.
            import os as _os
            _rd = float(regnet.REGNET_DIFF) + float(_os.environ.get("BISMUTH_TEST_DIFF_OFFSET", "0") or "0")
            return (float('%.10f' % _rd), float('%.10f' % (_rd - 8)), float(time_to_generate),
                    float(_rd), float(block_time), float(0), float(0), block_height)

        difficulty, hashrate, diff_adjustment, block_time = retarget(
            diff_block_previous, timestamp_last, timestamp_before_last, timestamp_1441, timestamp_1440)
        target = TARGET_BLOCK_TIME

        # hf2: past the activation height the LWMA retarget replaces the legacy controller's output
        # (symmetric, delicate, deterministically calculable — doc/18). Inert until a fork is signalled
        # and reached (node.fork_height is None otherwise), so every historical block is unchanged;
        # regnet never reaches here (it returns REGNET_DIFF above).
        fh = getattr(node, "fork_height", None)
        if fh is not None and (block_height + 1) >= fh:
            import difficulty_lwma
            solvetimes = _recent_solvetimes(db_handler, block_height, difficulty_lwma.WINDOW)
            if solvetimes:
                difficulty = quantize_ten(difficulty_lwma.lwma_next_difficulty(
                    solvetimes, diff_block_previous, target=float(TARGET_BLOCK_TIME)))

        #fork handling
        if node.is_mainnet:
            if block_height == fork.POW_FORK - fork.FORK_AHEAD:
                fork.limit_version(node)
        #fork handling

        diff_drop_time = DIFF_DROP_TIME

        if Decimal(time.time()) > Decimal(timestamp_last) + Decimal(2 * diff_drop_time):
            # Emergency diff drop
            time_difference = quantize_two(time.time()) - quantize_two(timestamp_last)
            diff_dropped = quantize_ten(difficulty) - quantize_ten(1) \
                           - quantize_ten(10 * (time_difference - 2 * diff_drop_time) / diff_drop_time)
        elif Decimal(time.time()) > Decimal(timestamp_last) + Decimal(diff_drop_time):
            time_difference = quantize_two(time.time()) - quantize_two(timestamp_last)
            diff_dropped = quantize_ten(difficulty) + quantize_ten(1) - quantize_ten(time_difference / diff_drop_time)
        else:
            diff_dropped = difficulty

        if difficulty < MIN_DIFFICULTY:
            difficulty = MIN_DIFFICULTY
        if diff_dropped < MIN_DIFFICULTY:
            diff_dropped = MIN_DIFFICULTY

        return (
            float('%.10f' % difficulty), float('%.10f' % diff_dropped), float(time_to_generate), float(diff_block_previous),
            float(block_time), float(hashrate), float(diff_adjustment),
            block_height)
    except Exception as e:
        # The [24,...] sentinel (below MIN_DIFFICULTY) is ONLY valid for a genuinely empty/bootstrap
        # chain (new chain / fresh regnet), where the window queries legitimately return no rows.
        # On a MATURE chain a transient failure here (a store read error, a None fetch, a bug in the LWMA
        # branch) must NOT silently collapse the acceptance threshold
        # to 24 — digest feeds diff[0] straight into the PoW check, so that would validate trivially
        # mined blocks. Fail closed instead so the digest attempt is retried rather than accepted.
        tip = getattr(node, "last_block", 0) or 0
        if tip < DIFFICULTY_WINDOW:
            return [24, 24, 0, 0, 0, 0, 0, 0]
        try:
            node.logger.app_log.warning(
                "difficulty(): retarget failed on a mature chain (tip {}), failing closed: {}".format(tip, e))
        except Exception:
            pass
        raise
