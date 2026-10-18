"""
LWMA difficulty gate (difficulty._recent_solvetimes + the fork_height override in difficulty()).

_recent_solvetimes derives the inter-block intervals the LWMA retarget consumes, from the miner-tx
(reward!=0) timestamps — the same data the legacy controller reads. The gate itself
(block_height+1 >= fork_height → LWMA, else legacy) is INERT until a fork is signalled and reached
(node.fork_height is None otherwise) and is bypassed on regnet (fixed REGNET_DIFF), so the full suite
staying replay-green with it added is the safety proof.

Run with: python3 -m pytest tests/test_difficulty_gate.py -v
"""
import difficulty


class _FakeLedger:
    """A ledger stand-in: the coinbase (miner tx) timestamps of the newest blocks ending at ``tip``."""

    def __init__(self, timestamps, tip=1000):
        start = tip - len(timestamps) + 1
        self._cb = {start + i: (start + i, float(ts)) for i, ts in enumerate(timestamps)}

    def first(self):
        return min(self._cb) if self._cb else None

    def tip(self):
        return max(self._cb) if self._cb else None

    def coinbase(self, height):
        return self._cb.get(int(height))


def test_recent_solvetimes_from_miner_timestamps():
    # miner-tx timestamps 100,160,260,300 -> solvetimes 60,100,40 (oldest→newest)
    db = _FakeLedger(["100", "160", "260", "300"])
    assert difficulty._recent_solvetimes(db, block_height=1000, window=60) == [60.0, 100.0, 40.0]


def test_recent_solvetimes_empty_or_single():
    assert difficulty._recent_solvetimes(_FakeLedger([]), 1000, 60) == []
    assert difficulty._recent_solvetimes(_FakeLedger(["100"]), 1000, 60) == []
