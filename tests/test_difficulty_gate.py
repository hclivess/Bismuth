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


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self.c = _FakeCursor(rows)

    def execute_param(self, cursor, sql, params):
        pass  # the fake just returns its canned rows from fetchall()


def test_recent_solvetimes_from_miner_timestamps():
    # miner-tx timestamps 100,160,260,300 -> solvetimes 60,100,40 (oldest→newest)
    db = _FakeDb([("100",), ("160",), ("260",), ("300",)])
    assert difficulty._recent_solvetimes(db, block_height=1000, window=60) == [60.0, 100.0, 40.0]


def test_recent_solvetimes_empty_or_single():
    assert difficulty._recent_solvetimes(_FakeDb([]), 1000, 60) == []
    assert difficulty._recent_solvetimes(_FakeDb([("100",)]), 1000, 60) == []
