"""Proof-of-work difficulty retargeting.

Computes the network difficulty the next block must satisfy, from the recent
history of block solve times in the ledger. The retarget steers the average
block interval toward the target by raising difficulty when blocks arrive too
fast and lowering it when they arrive too slowly, with the exact formula
selected per fork era (via ``fork``) and special-cased for regnet. Because the
returned value is the threshold ``digest`` checks the proof of work against,
this calculation is consensus-critical and must be deterministic across nodes.
"""

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


def _recent_solvetimes(db_handler, block_height, window):
    """The last ~window inter-block solvetimes (seconds), oldest→newest, from the miner-tx (reward!=0)
    timestamps — exactly the data the legacy controller already reads. Fed to the LWMA retarget."""
    db_handler.execute_param(
        db_handler.c,
        "SELECT timestamp FROM transactions WHERE block_height > ? AND reward != 0 ORDER BY block_height ASC",
        (block_height - window - 1,))
    ts = [float(r[0]) for r in db_handler.c.fetchall()]
    return [ts[i] - ts[i - 1] for i in range(1, len(ts))]


def difficulty(node, db_handler):
    try:
        fork = Fork()

        db_handler.execute(db_handler.c, "SELECT * FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 2")
        result = db_handler.c.fetchone()

        timestamp_last = Decimal(result[1])
        block_height = int(result[0])

        node.last_block_timestamp = timestamp_last
        #node.last_block = block_height do not fetch this here, could interfere with block saving

        previous = db_handler.c.fetchone()

        node.last_block_ago = int(time.time() - int(timestamp_last))

        # Failsafe for regtest starting at block 1}
        timestamp_before_last = timestamp_last if previous is None else Decimal(previous[1])

        db_handler.execute_param(db_handler.c, (
            "SELECT timestamp FROM transactions WHERE block_height > ? AND reward != 0 ORDER BY block_height ASC LIMIT 2"),
                                 (block_height - DIFFICULTY_WINDOW - 1,))
        timestamp_1441 = Decimal(db_handler.c.fetchone()[0])
        block_time_prev = (timestamp_before_last - timestamp_1441) / DIFFICULTY_WINDOW
        temp = db_handler.c.fetchone()
        timestamp_1440 = timestamp_1441 if temp is None else Decimal(temp[0])
        block_time = Decimal(timestamp_last - timestamp_1440) / DIFFICULTY_WINDOW

        db_handler.execute(db_handler.c, "SELECT difficulty FROM misc ORDER BY block_height DESC LIMIT 1")
        diff_block_previous = Decimal(db_handler.c.fetchone()[0])

        time_to_generate = timestamp_last - timestamp_before_last

        if node.is_regnet:
            # TEST-ONLY divergence injection for the #23 regnet soak (doc/35): BISMUTH_TEST_DIFF_OFFSET shifts
            # THIS node's reported regnet difficulty so a soak can make one node genuinely diverge from its
            # peers. Inert in prod — mainnet never takes the is_regnet branch — and 0 unless the env var is set.
            import os as _os
            _rd = float(regnet.REGNET_DIFF) + float(_os.environ.get("BISMUTH_TEST_DIFF_OFFSET", "0") or "0")
            return (float('%.10f' % _rd), float('%.10f' % (_rd - 8)), float(time_to_generate),
                    float(_rd), float(block_time), float(0), float(0), block_height)

        hashrate = pow(2, diff_block_previous / Decimal(2.0)) / (
                block_time * math.ceil(28 - diff_block_previous / Decimal(16.0)))
        # Calculate new difficulty for the desired block time
        target = TARGET_BLOCK_TIME
        ##D0 = diff_block_previous
        difficulty_new = Decimal(
            (2 / math.log(2)) * math.log(hashrate * target * math.ceil(28 - diff_block_previous / Decimal(16.0))))
        # Feedback controller
        Kd = KD_GAIN
        difficulty_new = difficulty_new - Kd * (block_time - block_time_prev)
        diff_adjustment = (difficulty_new - diff_block_previous) / DIFF_ADJUST_DIVISOR  # reduce by factor of 720

        if diff_adjustment > MAX_DIFF_ADJUST:
            diff_adjustment = MAX_DIFF_ADJUST

        difficulty_new_adjusted = quantize_ten(diff_block_previous + diff_adjustment)
        difficulty = difficulty_new_adjusted

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
            block_height)  # need to keep float here for database inserts support
    except: #new chain or regnet
        difficulty = [24,24,0,0,0,0,0,0]
        return difficulty
