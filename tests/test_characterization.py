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


# --- rollback checkpoint depth (configurable; default preserves historical behavior) -------------

def test_checkpoint_set_depth_default_and_configurable():
    from essentials import checkpoint_set, round_down

    class _Log:
        class app_log:
            @staticmethod
            def warning(*a, **k):
                pass

    class _N:
        last_block = 1450100
        checkpoint = 0
        logger = _Log()

    n = _N()
    checkpoint_set(n)  # default depth 30 == historical behavior
    assert n.checkpoint == round_down(1450100, 30) - 30

    deep = _N()
    deep.rollback_depth = 720
    checkpoint_set(deep)  # operator-raised depth lets the node roll back further
    assert deep.checkpoint == round_down(1450100, 720) - 720


def test_rollback_allowed_policy():
    from essentials import rollback_allowed

    class _Peers:
        def __init__(self, size, pct, reputable=2):
            self._size = size
            self.consensus_percentage = pct
            self.reputable_count = reputable    # proven (positive-reputation) peers

        @property
        def consensus_size(self):
            return self._size

    class _N:
        checkpoint = 1000
        rollback_consensus = False
        rollback_consensus_min_peers = 3
        rollback_consensus_threshold = 75
        rollback_consensus_min_reputable = 1
        peers = None

    n = _N()
    # shallow rollbacks (at/above the checkpoint) are always allowed
    assert rollback_allowed(n, 1000) is True
    assert rollback_allowed(n, 1500) is True
    # deep rollback, auto-recovery explicitly disabled -> refused
    assert rollback_allowed(n, 900) is False
    # deep rollback, auto-recovery enabled but consensus insufficient -> refused
    n.rollback_consensus = True
    n.peers = _Peers(size=2, pct=90)    # too few peers
    assert rollback_allowed(n, 900) is False
    n.peers = _Peers(size=5, pct=50)    # majority too weak
    assert rollback_allowed(n, 900) is False
    n.peers = _Peers(size=1, pct=100)   # a single (possibly malicious) peer cannot force a deep reorg
    assert rollback_allowed(n, 900) is False
    # anti-sybil gate: enough peers + strong majority BUT no proven peers (fresh sybil flood) -> refused
    n.peers = _Peers(size=5, pct=80, reputable=0)
    assert rollback_allowed(n, 900) is False
    # deep rollback with a strong supermajority over enough peers INCLUDING proven ones -> AUTO-RECOVER
    n.peers = _Peers(size=5, pct=80, reputable=2)
    assert rollback_allowed(n, 900) is True


# --- frozen consensus serialization boundary (doc/16 phase 1) ------------------------------------

def test_consensus_signature_buffer_is_frozen():
    import bismuth_serialize
    # The exact bytes a transaction signature is computed over. Changing this forks the network.
    buf = bismuth_serialize.signature_buffer("1500000000.00", "sender", "recipient",
                                             "1.00000000", "op", "data")
    assert buf == b"('1500000000.00', 'sender', 'recipient', '1.00000000', 'op', 'data')"


def test_consensus_block_hash_is_frozen():
    import hashlib
    import bismuth_serialize
    txs = [("1500000000.00", "a", "r", "0.00000000", "sig", "pk", "0", "of")]
    prev = "0" * 56
    # must equal the canonical legacy formula exactly
    assert (bismuth_serialize.block_hash(txs, prev)
            == hashlib.sha224((str(txs) + prev).encode("utf-8")).hexdigest())


# --- hf2 post-fork BINARY serialization (doc/29) — pin the v2 codec so it can't drift either --------

def test_consensus_signature_buffer_v2_is_frozen():
    import bismuth_serialize
    # canonical little-endian binary pre-image: MAGIC|VERSION|ts_cs(u64)|amount(u64)|lp(addr,recip,op)|lp(of,u32)
    buf = bismuth_serialize.signature_buffer_v2(150000000000, "Bis1aaaa", "bbbb",
                                                100000000, "vm:call", "x")
    assert buf.hex() == ("b201005cb2ec2200000000e1f50500000000"
                         "084269733161616161046262626207766d3a63616c6c0100000078")
    assert buf[0] == bismuth_serialize.V2_MAGIC and buf[1] == bismuth_serialize.V2_VERSION


def test_consensus_tx_id_v2_is_frozen():
    import bismuth_serialize
    assert (bismuth_serialize.tx_id_v2(150000000000, "Bis1aaaa", "bbbb", 100000000, "vm:call", "x")
            == "53b11e1ba5cfb4194ca66063a9944e9a46588a6b434bae634878cff841164a20")


def test_v2_and_legacy_preimages_cannot_alias():
    # a v2 pre-image starts with 0xB2; a legacy one starts with ASCII '(' (0x28) — so even an un-gated
    # mixup can never produce the same bytes for the two encodings.
    import bismuth_serialize
    v2 = bismuth_serialize.signature_buffer_v2(150000000000, "a", "b", 100000000, "op", "d")
    legacy = bismuth_serialize.signature_buffer("1500000000.00", "a", "b", "1.00000000", "op", "d")
    assert v2[0] == 0xB2 and legacy[0] == 0x28 and v2 != legacy
