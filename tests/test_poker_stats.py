"""tests/test_poker_stats.py — Stage-4 accounts + leaderboard INDEXER (doc/28 §7).

Drives contracts/poker_table.py through real hands on bismuth_riscv.execute (same harness style as
tests/test_poker_table_vm.py), then feeds the resulting committed STORAGE DICT to poker_stats.py — the
off-chain, read-only indexer — and asserts:
  * the append-only TAG_RESULT log is written during _settle (one record per participating seat),
  * the per-account fold (keyed by FULL 28-byte address [SEC-C1]) credits winners, conserves net chips,
    and tracks hands_played / hands_won / biggest_pot / tables,
  * the leaderboard ranks correctly by net / hands_won,
  * the INCREMENTAL cache does no recompute when S_RESULTCNT is unchanged and tops up only new records,
  * a reorg shrink triggers a full rebuild (no double counting),
  * the /api/poker/* JSON shapes (leaderboard / account / table results).

The indexer reads CONTRACT STATE only (an in-memory dict here; REST GET /api/vm/contract or node VM state in
production) — it NEVER scans the ledger.

Run: python3 -m pytest tests/test_poker_stats.py -v
"""
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "contracts"), os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import bismuth_riscv as rv
import poker_table as pt
import poker_stats as ps


def be4(x):
    return (x & 0xFFFFFFFF).to_bytes(4, "big")


def addr28(low32, salt=0):
    """A FULL 28-byte address whose low 32 bits == low32 (the authenticated SYS_CALLER word). `salt` fills the
    upper words so distinct players have distinct full identities even if low32 collided (it won't here)."""
    upper = (salt & 0xFFFFFFFF).to_bytes(4, "big") * 6
    return upper + (low32 & 0xFFFFFFFF).to_bytes(4, "big")


def card(rank, suit):
    return suit * 13 + rank


def commit_of(cards, nonce):
    return hashlib.sha256(bytes(cards) + nonce).digest()


HOLD_BEST = [card(12, 0), card(12, 1), card(12, 2), card(12, 3), card(11, 0)]   # quad aces
HOLD_MID = [card(10, 0), card(10, 1), card(10, 2), card(9, 0), card(8, 0)]      # trip queens
HOLD_WORST = [card(2, 1), card(4, 2), card(6, 3), card(8, 1), card(10, 3)]      # high card


class TableDriver:
    """Persistent-storage harness around poker_table bytecode (mirrors test_poker_table_vm.TableDriver), but
    sits players with FULL 28-byte addresses so the indexer's [SEC-C1] keying is exercised end-to-end."""

    def __init__(self, nseats, sb, bb, max_buyin=10000):
        self.code = pt.build(nseats, sb, bb, max_buyin=max_buyin)
        self.st = {}
        self.custody = 0
        self.height = 100
        self.addrs = {}        # seat -> full 28-byte address

    def call(self, sel, caller, tail=b"", value=0):
        return rv.execute(self.code, calldata=be4(sel) + tail, caller=caller, callvalue=value,
                          storage=dict(self.st), block_height=self.height,
                          self_balance=self.custody + value)

    def ok(self, sel, caller, tail=b"", value=0):
        r = self.call(sel, caller, tail, value)
        assert r.success, "expected success, got revert (sel %d)" % sel
        self.st = r.storage
        self.custody += value
        for _to, amt in r.transfers:
            self.custody -= amt
        self._last = r
        return r

    def slot(self, k):
        return self.st.get(k, 0)

    def sit(self, seat, low32, buyin, salt=0):
        a28 = addr28(low32, salt)
        self.addrs[seat] = a28
        self.ok(pt.FN_SIT, low32, be4(seat) + a28, value=buyin)

    def deal(self):
        self.ok(pt.FN_DEAL, 0xDEAD)

    def commit(self, low32, cards, nonce):
        self.ok(pt.FN_COMMIT, low32, commit_of(cards, nonce))

    def reveal(self, low32, cards, nonce):
        self.ok(pt.FN_REVEAL, low32, bytes(cards) + nonce)

    def phase(self):
        return self.slot(pt.S_PHASE)

    def toact(self):
        return self.slot(pt.S_TOACT)

    def escrow(self):
        return self.slot(pt.S_ESCROW)

    def storage_dict(self):
        """The committed contract storage as the indexer sees it via /api/vm/contract ({int_key:int_val})."""
        return {int(k): int(v) for k, v in self.st.items()}


# map seat -> low32, set per test, used by the check-down helper
_SEATLOW = {}


def _set_seats(*lows):
    _SEATLOW.clear()
    for i, lo in enumerate(lows):
        _SEATLOW[i] = lo


def _check_to_showdown(d):
    guard = 0
    while d.phase() == pt.PH_BET:
        d.ok(pt.FN_CHECK, _SEATLOW[d.toact()])
        guard += 1
        assert guard < 100


def _dict_reader(storage):
    """An injected reader (like REST GET /api/vm/contract, but in-memory) keyed by table address."""
    def _r(addr):
        return dict(storage.get(addr, {}))
    return _r


# ============================================================ one heads-up hand: log + fold
def test_single_hand_results_logged_and_folded():
    d = TableDriver(2, 1, 2)
    _set_seats(0xA0, 0xA1)
    d.sit(0, 0xA0, 100)
    d.sit(1, 0xA1, 100)
    d.deal()
    d.ok(pt.FN_CALL, 0xA0)
    d.ok(pt.FN_CHECK, 0xA1)
    _check_to_showdown(d)
    n0, n1 = bytes(range(32)), bytes(reversed(range(32)))
    d.commit(0xA0, HOLD_BEST, n0)
    d.commit(0xA1, HOLD_WORST, n1)
    d.reveal(0xA0, HOLD_BEST, n0)
    d.reveal(0xA1, HOLD_WORST, n1)
    d.ok(pt.FN_SETTLE, 0xDEAD)

    sto = d.storage_dict()
    # the append-only log was written during _settle: 2 participating seats -> 2 records, high-water == 2.
    assert pt.result_count(sto) == 2, pt.result_count(sto)
    recs = [pt.read_result(sto, s) for s in range(2)]
    by_seat = {r["seat"]: r for r in recs}
    assert by_seat[0]["hand_no"] == 1 and by_seat[1]["hand_no"] == 1
    assert by_seat[0]["payout"] == 4 and by_seat[0]["contrib"] == 2   # winner: +pot 4, put in 2
    assert by_seat[1]["payout"] == 0 and by_seat[1]["contrib"] == 2   # loser: won 0, put in 2

    TBL = "tableA"
    reader = _dict_reader({TBL: sto})
    state = ps.cold_build(reader, [TBL])

    a0 = ps.account(state, addr28(0xA0).hex())
    a1 = ps.account(state, addr28(0xA1).hex())
    # FULL 28-byte address keying [SEC-C1]
    assert a0["address"] == addr28(0xA0).hex() and len(a0["address"]) == 56
    assert a0["hands_played"] == 1 and a0["hands_won"] == 1
    assert a0["won"] == 4 and a0["wagered"] == 2 and a0["net"] == 2
    assert a0["biggest_pot"] == 4
    assert a1["hands_played"] == 1 and a1["hands_won"] == 0
    assert a1["won"] == 0 and a1["wagered"] == 2 and a1["net"] == -2
    # net conserves to zero across the hand
    assert a0["net"] + a1["net"] == 0
    assert a0["tables"] == [TBL] and a1["tables"] == [TBL]


# ============================================================ multi-hand leaderboard + net conservation
def _play_headsup_hand(d, winner_low, loser_low, winner_seat, loser_seat, nonce_byte):
    """Play one heads-up check/call-to-showdown hand where winner_seat holds the best hand."""
    d.deal()
    # whoever is to-act first just calls/checks down
    first = d.toact()
    d.ok(pt.FN_CALL, _SEATLOW[first]) if d.phase() == pt.PH_BET else None
    _check_to_showdown(d)
    nw, nl = bytes([nonce_byte]) * 32, bytes([nonce_byte ^ 0xFF]) * 32
    d.commit(winner_low, HOLD_BEST, nw)
    d.commit(loser_low, HOLD_WORST, nl)
    d.reveal(winner_low, HOLD_BEST, nw)
    d.reveal(loser_low, HOLD_WORST, nl)
    d.ok(pt.FN_SETTLE, 0xDEAD)


def test_multi_hand_leaderboard_and_net_conservation():
    d = TableDriver(2, 1, 2)
    _set_seats(0xA0, 0xA1)
    d.sit(0, 0xA0, 1000)
    d.sit(1, 0xA1, 1000)
    # seat0 wins hand 1; seat1 wins hand 2; seat0 wins hand 3.
    _play_headsup_hand(d, 0xA0, 0xA1, 0, 1, 0x10)
    _play_headsup_hand(d, 0xA1, 0xA0, 1, 0, 0x20)
    _play_headsup_hand(d, 0xA0, 0xA1, 0, 1, 0x30)

    sto = d.storage_dict()
    TBL = "tableB"
    state = ps.cold_build(_dict_reader({TBL: sto}), [TBL])

    a0 = ps.account(state, addr28(0xA0).hex())
    a1 = ps.account(state, addr28(0xA1).hex())
    assert a0["hands_played"] == 3 and a1["hands_played"] == 3
    assert a0["hands_won"] == 2 and a1["hands_won"] == 1
    # net chips conserve across the table (zero-sum)
    assert a0["net"] + a1["net"] == 0
    assert a0["net"] > 0 and a1["net"] < 0      # seat0 net winner

    lb_net = ps.leaderboard(state, metric="net", top=10)
    assert lb_net[0]["address"] == addr28(0xA0).hex()      # top net = seat0
    assert lb_net[1]["address"] == addr28(0xA1).hex()
    lb_won = ps.leaderboard(state, metric="hands_won", top=10)
    assert lb_won[0]["address"] == addr28(0xA0).hex() and lb_won[0]["hands_won"] == 2


# ============================================================ incremental cache: no recompute when unchanged
def test_incremental_cache_no_recompute_when_resultcnt_unchanged():
    d = TableDriver(2, 1, 2)
    _set_seats(0xA0, 0xA1)
    d.sit(0, 0xA0, 1000)
    d.sit(1, 0xA1, 1000)
    _play_headsup_hand(d, 0xA0, 0xA1, 0, 1, 0x11)

    sto = d.storage_dict()
    TBL = "tableC"
    reader = _dict_reader({TBL: sto})
    state = ps.cold_build(reader, [TBL])
    cnt_after_first = state["tables"][TBL]["resultcnt"]
    a0_net = ps.account(state, addr28(0xA0).hex())["net"]

    # advance with the SAME storage (S_RESULTCNT unchanged) -> 0 new records folded, totals identical.
    folded = ps._credit_results(state, reader, TBL)
    assert folded == 0
    assert state["tables"][TBL]["resultcnt"] == cnt_after_first
    assert ps.account(state, addr28(0xA0).hex())["net"] == a0_net

    # play another hand -> S_RESULTCNT grows -> advance folds ONLY the new records.
    _play_headsup_hand(d, 0xA1, 0xA0, 1, 0, 0x22)
    sto2 = d.storage_dict()
    reader2 = _dict_reader({TBL: sto2})
    new_cnt = pt.result_count(sto2)
    assert new_cnt > cnt_after_first
    folded2 = ps._credit_results(state, reader2, TBL)
    assert folded2 == (new_cnt - cnt_after_first)        # only the new records, no rescan
    # incremental result equals a fresh cold build over the full log
    cold = ps.cold_build(reader2, [TBL])
    assert ps.account(state, addr28(0xA0).hex()) == ps.account(cold, addr28(0xA0).hex())
    assert ps.account(state, addr28(0xA1).hex()) == ps.account(cold, addr28(0xA1).hex())


# ============================================================ reorg shrink -> full rebuild, no double count
def test_reorg_shrink_triggers_rebuild():
    d = TableDriver(2, 1, 2)
    _set_seats(0xA0, 0xA1)
    d.sit(0, 0xA0, 1000)
    d.sit(1, 0xA1, 1000)
    _play_headsup_hand(d, 0xA0, 0xA1, 0, 1, 0x33)
    _play_headsup_hand(d, 0xA1, 0xA0, 1, 0, 0x44)
    sto_full = d.storage_dict()
    TBL = "tableD"

    state = ps.cold_build(_dict_reader({TBL: sto_full}), [TBL])
    won_full = ps.account(state, addr28(0xA0).hex())["hands_won"]

    # simulate a reorg that rolled back the second hand: S_RESULTCNT shrinks. advance() must NOT double-count;
    # it detects the shrink and rebuilds from the canonical (shorter) log.
    sto_short = dict(sto_full)
    # drop the records of the most recent hand by lowering the high-water + clearing those rows
    cnt = pt.result_count(sto_full)
    sto_short[pt.S_RESULTCNT] = cnt - 2
    for s in (cnt - 1, cnt - 2):
        for w in range(pt.RESULT_WORDS):
            sto_short.pop(pt.result_key(s, w), None)

    state2 = ps.advance(state, _dict_reader({TBL: sto_short}), [TBL])
    # after the rebuild the totals match a cold build over the shortened log (no stale double-counting)
    cold_short = ps.cold_build(_dict_reader({TBL: sto_short}), [TBL])
    assert ps.account(state2, addr28(0xA0).hex()) == ps.account(cold_short, addr28(0xA0).hex())
    assert ps.account(state2, addr28(0xA0).hex())["hands_won"] <= won_full


# ============================================================ multi-table: tables-seen + scoped reads
def test_multi_table_and_table_results_shape():
    # two separate tables; the SAME player (full address) sits at both.
    dA = TableDriver(2, 1, 2)
    _set_seats(0xB0, 0xB1)
    dA.sit(0, 0xB0, 500)
    dA.sit(1, 0xB1, 500)
    _play_headsup_hand(dA, 0xB0, 0xB1, 0, 1, 0x55)

    dB = TableDriver(2, 1, 2)
    _set_seats(0xB0, 0xB1)
    dB.sit(0, 0xB0, 500)
    dB.sit(1, 0xB1, 500)
    _play_headsup_hand(dB, 0xB1, 0xB0, 1, 0, 0x66)

    storages = {"t1": dA.storage_dict(), "t2": dB.storage_dict()}
    reader = _dict_reader(storages)
    state = ps.cold_build(reader, ["t1", "t2"])

    a = ps.account(state, addr28(0xB0).hex())
    assert a["tables"] == ["t1", "t2"]           # both tables seen
    assert a["hands_played"] == 2 and a["hands_won"] == 1

    # /api/poker/table/<addr>/results JSON shape (scoped to one table, state-only read)
    tr = ps.table_results(reader, "t1", since=0, limit=100)
    assert tr["table"] == "t1" and tr["resultcnt"] == 2
    assert len(tr["results"]) == 2
    for r in tr["results"]:
        # records are self-describing: addr_hex (captured at settle) lets the indexer credit the right player
        # regardless of later seat reassignment [review #1]
        assert set(r.keys()) == {"seq", "hand_no", "seat", "payout", "contrib", "addr_hex"}
        assert len(r["addr_hex"]) == 56          # full 28-byte address [SEC-C1]


# ============================================================ endpoint JSON shapes via a fake node
class _FakeVMState:
    def __init__(self, storages):
        self.storages = storages

    def list_contracts(self):
        return list(self.storages.keys())

    def get_code(self, addr):
        return b"\x00" if addr in self.storages else None

    def load_storage(self, addr):
        return dict(self.storages.get(addr, {}))


class _FakeLogger:
    class _App:
        def warning(self, *a, **k):
            pass
    app_log = _App()


class _FakeNode:
    def __init__(self, storages, tmpdir):
        self.vm_state = _FakeVMState(storages)
        self.ledger_path = os.path.join(tmpdir, "ledger.db")
        self.logger = _FakeLogger()


def test_endpoints_json_shapes(tmp_path):
    d = TableDriver(2, 1, 2)
    _set_seats(0xC0, 0xC1)
    d.sit(0, 0xC0, 500)
    d.sit(1, 0xC1, 500)
    _play_headsup_hand(d, 0xC0, 0xC1, 0, 1, 0x77)
    sto = d.storage_dict()
    TBL = "0xpokertable"
    node = _FakeNode({TBL: sto}, str(tmp_path))

    # node_reader reads VM STATE only (never the ledger)
    reader = ps.node_reader(node)
    assert pt.result_count(reader(TBL)) == 2

    # first request: cache cold -> "computing", kicks the (synchronous-in-test) daemon
    r1 = ps.request_leaderboard(node, metric="net", top=10)
    assert r1["status"] in ("computing", "ok")
    # wait for the background maintainer to settle, then request again
    for _ in range(50):
        if not getattr(node, "_poker_maintaining", False) and getattr(node, "_poker_stats_cache", None):
            break
        import time
        time.sleep(0.02)
    r2 = ps.request_leaderboard(node, metric="net", top=10)
    assert r2["status"] == "ok"
    assert isinstance(r2["leaderboard"], list) and len(r2["leaderboard"]) == 2
    top = r2["leaderboard"][0]
    assert set(["address", "net", "won", "wagered", "hands_played", "hands_won",
                "biggest_pot", "tables"]).issubset(top.keys())
    assert top["address"] == addr28(0xC0).hex()      # net winner on top

    racc = ps.request_account(node, addr28(0xC0).hex())
    assert racc["status"] == "ok"
    assert racc["account"]["address"] == addr28(0xC0).hex()
    assert racc["account"]["net"] == 2

    rtab = ps.request_table_results(node, TBL, since=0, limit=10)
    assert rtab["status"] == "ok" and rtab["table"] == TBL and rtab["resultcnt"] == 2

    # the per-ledger JSON cache was written namespaced by the ledger filename
    cache_file = ps._cache_path(node)
    assert os.path.basename(cache_file) == "poker_poker-ledger.db.json"
    assert os.path.exists(cache_file)
