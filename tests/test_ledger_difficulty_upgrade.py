"""
Difficulty on the LMDB ledger, the genesis seed + ledger open, and the store upgrade (scripts/upgrade_ledger.py).

* ``difficulty.retarget`` is pure: steady state holds, faster blocks raise it, the per-block rise is capped.
* ``window_timestamps`` reads the right four mining timestamps, clamped to the first stored block.
* ``difficulty(node, db)`` == retarget over the store's last recorded difficulty (and the regnet constant).
* ``node_init.open_ledger`` seeds genesis, applies the mainnet allocations, refuses an old schema.
* ``rebuild_difficulty`` replays EXACTLY what the live node records, matches/re-anchors on peer anchors.

Run with: python3 -m pytest tests/test_ledger_difficulty_upgrade.py -v
"""
import importlib.util
import json
import os
import sys
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import amounts  # noqa: E402
import block_store  # noqa: E402
import difficulty as D  # noqa: E402
import ledger_genesis  # noqa: E402
from block_store import BlockStore  # noqa: E402
from dbhandler import DbHandler  # noqa: E402

SMALL = 1 << 27
M = "a" * 56


def _load_upgrade():
    spec = importlib.util.spec_from_file_location("upgrade_ledger", os.path.join(ROOT, "scripts", "upgrade_ledger.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cb(h, ts):
    return [(str(h), "%.2f" % ts, M, M, "0", "cb%d" % h, "PK", "%056x" % h, "0", "10", "0", "n%d" % h)]


def _build(path, n, spacing, start_ts=None, jitter=True, diff0=60.0):
    """n blocks of coinbases; per-block difficulty recorded the way the node does (difficulty(): retarget)."""
    s = BlockStore(path, map_size=SMALL)
    t = (time.time() - n * spacing) if start_ts is None else start_ts
    for h in range(1, n + 1):
        t += spacing + ((h * 7919) % 23 - 11 if jitter else 0)
        s.commit_block(h, "%056x" % h, _cb(h, t), difficulty=diff0 if h == 1 else None)
    return s


def _replay_like_the_node(s, upto, start=5, d0=60.0):
    """The node's recorded difficulty after each block from ``start`` (recorded as ``d0``): retarget on the
    previous record + window_timestamps. (Block 2's window has zero length; a replay always starts later.)"""
    d = Decimal(block_store.real_text(d0))
    out = {start: d}
    for h in range(start + 1, upto + 1):
        nd, *_ = D.retarget(d, *D.window_timestamps(s, h - 1))
        nd = max(nd, D.MIN_DIFFICULTY)
        d = Decimal(block_store.real_text(D.recorded(nd)))
        out[h] = d
    return out


# ---------------------------------------------------------------------------------------- retarget (pure)
def _window(block_time, prev_block_time=None):
    prev_block_time = block_time if prev_block_time is None else prev_block_time
    t1440 = Decimal(1_000_000)
    tl = t1440 + D.DIFFICULTY_WINDOW * Decimal(block_time)
    t1441 = Decimal(999_000)
    tbl = t1441 + D.DIFFICULTY_WINDOW * Decimal(prev_block_time)
    return tl, tbl, t1441, t1440


@pytest.mark.parametrize("d", [Decimal(60), Decimal("88.8928995217"), Decimal(110)])
def test_retarget_steady_state_at_target_block_time(d):
    nd, hashrate, adj, bt = D.retarget(d, *_window(60))
    assert bt == 60 and abs(adj) < Decimal("1e-9")
    assert abs(nd - d) < Decimal("1e-9")
    assert hashrate > 0


def test_retarget_direction_and_cap():
    d = Decimal(90)
    faster, *_ = D.retarget(d, *_window(30))
    slower, *_ = D.retarget(d, *_window(120))
    assert faster > d > slower
    # 2/ln2 * ln(60/30) = +2 bits -> /720 per block
    assert abs((faster - d) - Decimal(2) / 720) < Decimal("1e-8")
    capped, _h, adj, _bt = D.retarget(d, *_window(0.0001, 1000))
    assert adj == D.MAX_DIFF_ADJUST and capped == d + 1
    # the derivative term: speeding up vs the previous window pushes up harder
    accel, *_ = D.retarget(d, *_window(60, 90))
    assert accel > d


def test_retarget_is_deterministic():
    args = (Decimal("88.8928995217"),) + _window(59.3, 61.2)
    assert D.retarget(*args) == D.retarget(*args)


def test_recorded_form():
    assert D.recorded(Decimal("88.89289952171234")) == 88.8928995217
    assert block_store.real_text(D.recorded(Decimal(50))) == "50.0"


# ---------------------------------------------------------------------------------------- window on the store
def test_window_timestamps_full_and_clamped(tmp_path):
    s = _build(str(tmp_path / "w"), D.DIFFICULTY_WINDOW + 20, 60)
    try:
        h = s.tip()
        tl, tbl, t1441, t1440 = D.window_timestamps(s, h)
        assert tl == Decimal(D.coinbase_ts(s, h)) and tbl == Decimal(D.coinbase_ts(s, h - 1))
        assert t1441 == Decimal(D.coinbase_ts(s, h - 1440)) and t1440 == Decimal(D.coinbase_ts(s, h - 1439))
        # near the bottom the window clamps to the first stored block
        tl, tbl, t1441, t1440 = D.window_timestamps(s, 5)
        assert t1441 == Decimal(D.coinbase_ts(s, 1)) and t1440 == Decimal(D.coinbase_ts(s, 2))
        tl, tbl, t1441, t1440 = D.window_timestamps(s, 1)
        assert tl == tbl == t1441 == t1440
        assert D.coinbase_ts(s, 10 ** 6) is None
    finally:
        s.close()


def test_recent_solvetimes(tmp_path):
    s = _build(str(tmp_path / "st"), 30, 60, jitter=False)
    try:
        st = D._recent_solvetimes(DbHandler(s), 30, 10)
        assert len(st) == 10 and all(abs(x - 60) < 0.02 for x in st)
    finally:
        s.close()


# ---------------------------------------------------------------------------------------- difficulty(node, db)
class _Node:
    is_regnet = False
    is_mainnet = False
    fork_height = None
    last_block = 0

    class logger:
        class app_log:
            @staticmethod
            def warning(*a): pass


def test_difficulty_matches_retarget_on_the_store(tmp_path):
    s = _build(str(tmp_path / "d"), 200, 60)
    s.put_difficulties([(200, "90.5")])
    try:
        node = _Node()
        out = D.difficulty(node, DbHandler(s))
        expect, *_ = D.retarget(Decimal("90.5"), *D.window_timestamps(s, 200))
        assert out[0] == float('%.10f' % expect)
        assert out[3] == 90.5 and out[7] == 200
        assert node.last_block_timestamp == Decimal(D.coinbase_ts(s, 200))
        # the same through the bare store (as_ledger)
        assert D.difficulty(_Node(), s)[0] == out[0]
    finally:
        s.close()


def test_difficulty_regnet_constant(tmp_path):
    import regnet
    s = _build(str(tmp_path / "r"), 5, 60)
    try:
        node = _Node()
        node.is_regnet = True
        out = D.difficulty(node, DbHandler(s))
        assert out[0] == float(regnet.REGNET_DIFF) and out[7] == 5
    finally:
        s.close()


def test_difficulty_emergency_drop_after_a_stall(tmp_path):
    s = _build(str(tmp_path / "e"), 50, 60, start_ts=time.time() - 50 * 60 - 3600)   # tip ~1h old
    s.put_difficulties([(50, "90")])
    try:
        out = D.difficulty(_Node(), DbHandler(s))
        assert out[1] < out[0]                               # the broadcast (dropped) difficulty
    finally:
        s.close()


def test_difficulty_fails_closed_on_a_mature_chain():
    class Broken:
        store = None

    node = _Node()
    node.last_block = D.DIFFICULTY_WINDOW + 1
    with pytest.raises(Exception):
        D.difficulty(node, Broken())
    node.last_block = 3                                      # an empty/bootstrap chain gets the sentinel
    assert D.difficulty(node, Broken())[0] == 24


# ---------------------------------------------------------------------------------------- genesis + open_ledger
class _OpenNode:
    is_regnet = False
    is_mainnet = True
    sync_from_genesis = True                                 # no peer snapshot bootstrap in tests
    bootstrap_p2p = False

    class logger:
        class app_log:
            msgs = []

            @classmethod
            def warning(cls, m):
                cls.msgs.append(m)
            critical = warning


def test_genesis_rows():
    rows = ledger_genesis.rows()
    assert len(rows) == 1 and len(rows[0]) == 12
    r = block_store.typed_row(rows[0])
    assert r[0] == 1 and r[2] == "genesis" and r[3] == ledger_genesis.GENESIS_ADDRESS and r[9] == 1
    amounts.LEDGER_INTEGER = True
    assert ledger_genesis.rows()[0][9] == str(amounts.to_units(1))


def test_allocations_are_mainnet_only():
    assert ledger_genesis.allocations(_OpenNode()) == ledger_genesis.MAINNET_ALLOCATIONS
    assert ledger_genesis.allocations(_Node()) == {}
    assert ledger_genesis.allocations(object()) == {}
    # the concluded dev / hypernode totals, in units
    assert sum(ledger_genesis.MAINNET_ALLOCATIONS.values()) == 315147652720086 + 652116733333333


def test_seed_genesis_once(tmp_path):
    import chain_ops
    import regnet
    node = _OpenNode()
    node.is_mainnet, node.is_regnet = False, True
    node.ledger = BlockStore(str(tmp_path / "g"), map_size=SMALL)
    try:
        assert chain_ops.seed_genesis(node) is True
        assert chain_ops.seed_genesis(node) is False
        assert node.ledger.tip() == 1
        assert node.ledger.block_hash(1) == ledger_genesis.GENESIS_BLOCK_HASH
        assert node.ledger.difficulty(1) == str(regnet.REGNET_DIFF)
        assert node.ledger.allocations() == {}
        assert node.ledger.balance(ledger_genesis.GENESIS_ADDRESS) == 1
    finally:
        node.ledger.close()


def test_open_ledger_mainnet_seeds_allocates_and_checks_schema(tmp_path, monkeypatch):
    import kvstore
    import node_init
    node = _OpenNode()
    node.ledger_path = str(tmp_path / "ledger.db")
    path = kvstore.store_path(node.ledger_path, "blockstore")
    try:
        node_init.open_ledger(node)
        st = node.ledger
        assert node.block_store is st and st.tip() == 1 and st.schema() == block_store.SCHEMA
        assert st.difficulty(1) == "1"
        hn = "3e08b5538a4509d9daa99e01ca5912cda3e98a7f79ca01248c2bde16"
        assert st.balance(ledger_genesis.GENESIS_ADDRESS) == Decimal("3151477.52720086")   # + genesis reward 1
        assert st.balance(hn) == Decimal("6521167.33333333")

        # a changed allocation table is applied on the next open (exactly the difference)
        monkeypatch.setitem(ledger_genesis.MAINNET_ALLOCATIONS, hn, 652116733333333 + 5)
        node_init.open_ledger(node)
        assert st.balance(hn) == Decimal("6521167.33333338")

        # a ledger from before the LMDB-only schema is refused with the upgrade instruction
        st.set_schema(1)
        with pytest.raises(SystemExit):
            node_init.open_ledger(node)
        assert any("upgrade_ledger.py" in m for m in _OpenNode.logger.app_log.msgs)
    finally:
        block_store.close_ledger(path)


def test_open_ledger_regnet_has_no_allocations(tmp_path):
    import kvstore
    import node_init
    node = _OpenNode()
    node.is_mainnet, node.is_regnet = False, True
    node.ledger_path = str(tmp_path / "reg.db")
    path = kvstore.store_path(node.ledger_path, "blockstore")
    try:
        node_init.open_ledger(node)
        assert node.ledger.tip() == 1 and node.ledger.allocations() == {}
    finally:
        block_store.close_ledger(path)


# ---------------------------------------------------------------------------------------- upgrade_ledger
def test_rebuild_difficulty_reproduces_the_node(tmp_path):
    up = _load_upgrade()
    n = D.DIFFICULTY_WINDOW + 120
    s = _build(str(tmp_path / "u"), n, 57)
    try:
        truth = _replay_like_the_node(s, n)
        anchors = {5: float(truth[5]), 900: float(truth[900]), n: float(truth[n])}
        logs = []
        matched, mismatched = up.rebuild_difficulty(s, anchors, log=logs.append, batch=500)
        assert matched == 2 and mismatched == []
        for h in (6, 7, 1000, D.DIFFICULTY_WINDOW + 1, n):
            assert Decimal(s.difficulty(h)) == truth[h], h
        assert s.last_difficulty() == (n, str(truth[n]))
        assert any("difficulty" in m for m in logs)
    finally:
        s.close()


def test_rebuild_difficulty_reanchors_on_a_mismatch(tmp_path):
    up = _load_upgrade()
    s = _build(str(tmp_path / "m"), 300, 61)
    try:
        truth = _replay_like_the_node(s, 300)
        published = float(truth[150]) + 0.5                  # the network says otherwise (an old-era rule)
        matched, mismatched = up.rebuild_difficulty(s, {5: float(truth[5]), 150: published}, log=lambda m: None)
        assert matched == 0 and [m[0] for m in mismatched] == [150]
        assert float(s.difficulty(150)) == published         # the published value wins…
        assert Decimal(s.difficulty(151)) != truth[151]       # …and the replay continues from it
        assert s.difficulty(4) is None                       # nothing is written below the first anchor
        assert Decimal(s.difficulty(149)) == truth[149]
    finally:
        s.close()


def test_rebuild_difficulty_needs_an_anchor(tmp_path):
    up = _load_upgrade()
    s = _build(str(tmp_path / "n"), 10, 60)
    try:
        with pytest.raises(SystemExit):
            up.rebuild_difficulty(s, {}, log=lambda m: None)
        with pytest.raises(SystemExit):
            up.rebuild_difficulty(s, {999: 50.0}, log=lambda m: None)    # only above the tip
    finally:
        s.close()


def test_coinbase_ts_fast_matches_the_typed_read(tmp_path):
    up = _load_upgrade()
    from kvstore import Codec
    s = _build(str(tmp_path / "f"), 5, 60)
    try:
        with s.store.txn() as txn:
            rec = Codec.unpack(txn.get(s.blocks, Codec.hkey(4)))
        assert up._coinbase_ts_fast(rec) == D.coinbase_ts(s, 4)
    finally:
        s.close()


def test_fetch_anchors_merges_sources_first_wins(capsys):
    up = _load_upgrade()
    series = {"series": [{"height": 10, "difficulty": 90.1}, {"height": 20, "difficulty": "90.2"}]}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(series if self.path == "/api/stats/difficulty" else {}).encode()
            self.send_response(200 if self.path == "/api/stats/difficulty" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d/" % srv.server_port
        anchors = up.fetch_anchors(["http://127.0.0.1:1", url], timeout=5)
        assert anchors == {10: 90.1, 20: 90.2}
        assert "unavailable" in capsys.readouterr().out
    finally:
        srv.shutdown()


def test_upgrade_cli_reindexes_and_sets_the_schema(tmp_path):
    import subprocess
    p = str(tmp_path / "cli")
    s = _build(p, 20, 60)
    s.set_schema(1)
    s.close()
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "upgrade_ledger.py"), p,
                          "--skip-difficulty"], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    s = BlockStore(p, map_size=SMALL)
    try:
        assert s.schema() == block_store.SCHEMA
        assert s.tx_count() == 20 and s.balance(M) == 200
    finally:
        s.close()
