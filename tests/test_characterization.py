"""
Characterization tests for consensus-critical pure-ish functions.

These pin the *current* outputs so behavior-preserving refactors (e.g. extracting magic numbers into
named constants) can be proven not to change consensus. No running node required: difficulty() is
driven against a synthetic in-memory ledger.
"""
import sqlite3

import difficulty
import mining_heavy3
from essentials import fee_calculate


# --- difficulty() against a synthetic ledger ---------------------------------

class _Node:
    is_regnet = False
    is_mainnet = False   # avoids fork.limit_version on a synthetic chain
    is_testnet = True
    last_block_timestamp = 0
    last_block_ago = 0


class _DB:
    def __init__(self, conn):
        self.conn = conn
        self.c = conn.cursor()

    def execute(self, cursor, query):
        cursor.execute(query)

    def execute_param(self, cursor, query, param):
        cursor.execute(query, param)


def _ledger(gaps, diff="65"):
    """Build an in-memory ledger: block 1 at a fixed base ts, then `gaps` inter-block seconds."""
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, "
                "amount, signature, public_key, block_hash, fee, reward, operation, openfield)")
    cur.execute("CREATE TABLE misc (block_height INTEGER, difficulty)")
    ts = 1500000000.0
    for height, gap in enumerate([0] + list(gaps), start=1):
        ts += gap
        cur.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (height, "%.2f" % ts, "a", "r", "0", "s%d" % height, "p", "b", "0", "1", "0", ""))
        cur.execute("INSERT INTO misc VALUES (?,?)", (height, diff))
    conn.commit()
    return conn


def _difficulty(gaps):
    return difficulty.difficulty(_Node(), _DB(_ledger(gaps)))


def test_difficulty_equilibrium_60s():
    d = _difficulty([60] * 1499)
    assert d[3] == 65.0                            # previous difficulty
    assert abs(d[4] - 59.95833333333) < 1e-6       # rolling block time
    assert abs(d[0] - 65.0000027839) < 1e-9        # new difficulty
    assert abs(d[5] - 4220987.4912801245) < 1e-3   # hashrate
    assert abs(d[6] - 2.783943303816569e-06) < 1e-12  # adjustment


def test_difficulty_fast_blocks_50s():
    d = _difficulty([50] * 1499)
    assert abs(d[0] - 65.0007334351) < 1e-9
    assert abs(d[5] - 5065184.989536149) < 1e-3
    assert abs(d[6] - 0.000733435070619907) < 1e-12


def test_difficulty_non_uniform_exercises_pd_term():
    # block_time != block_time_prev here, so the Kd feedback term is exercised.
    d = _difficulty([70] * 749 + [50] * 750)
    assert abs(d[0] - 65.0002240989) < 1e-9
    assert abs(d[5] - 4251021.346053027) < 1e-3
    assert abs(d[6] - 0.00022409892620493785) < 1e-12


# --- mining (pure, deterministic regnet helpers) -----------------------------

def test_bin_convert():
    assert mining_heavy3.bin_convert("a") == "01100001"          # ord('a') == 97
    assert mining_heavy3.bin_convert("ab") == "0110000101100010"


def test_anneal3_regnet_is_identity_hex():
    out = mining_heavy3.anneal3_regnet(None, 255)
    assert len(out) == 56
    assert out.endswith("ff")
    assert set(out[:-2]) == {"0"}


# --- fee formula (consensus) -------------------------------------------------

def test_fee_calculate_cases():
    assert str(fee_calculate("")) == "0.01000000"
    assert str(fee_calculate("x" * 50)) == "0.01050000"
    assert str(fee_calculate("alias=name")) == "1.01010000"
    assert str(fee_calculate("", "token:issue")) == "10.01000000"
    assert str(fee_calculate("", "token:transfer")) == "0.01000000"  # no surcharge for transfers
