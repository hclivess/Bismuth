"""poker_stats.py — Stage-4 accounts + leaderboard INDEXER for the Bismuth poker tables (doc/28 §7).

This is an OFF-CHAIN, READ-ONLY, consensus-NEUTRAL derived view. It is NOT a contract and it NEVER gates a
single chip — money only moves through contracts/poker_table.py's checked transfers + closing invariant. The
leaderboard is a pure, reproducible fold over canonical on-chain data: the append-only TAG_RESULT log each
table writes during _settle (one 4-word record per paid seat: hand_no, seat, payout, pot total), keyed by the
seat's FULL 28-byte address [SEC-C1].

HARD CONSTRAINT (see MEMORY: "No heavy scans on prod ledger"): this module NEVER opens, scans, or queries
static/ledger.db or hyper.db. It reads ONLY contract STATE, via an injected `reader(addr) -> {int_key:int_val}`
that is backed by either:
  * the node VM state (`node.vm_state.load_storage(addr)` — committed contract storage, no ledger I/O), or
  * the REST read path GET /api/vm/contract/<addr> (state-root-backed), or
  * an in-memory dict (tests).
It mirrors rest_stats.py / token_index.py: a per-ledger JSON cache namespaced by the ledger filename, advanced
INCREMENTALLY from S_RESULTCNT (only result records >= the cached high-water are read — never a rescan), with
the request path returning the cached snapshot immediately while a single guarded background daemon tops it up.

[SEC-M2] reorg-safe + deduped by (contract, block, seq): an append-only log can only grow within a canonical
chain; on a reorg the high-water for a table can DROP (records were rolled back), so the maintainer detects a
shrunk S_RESULTCNT and rebuilds that table's slice from scratch (idempotent fold) rather than double-counting.
"""
import json
import os
import sys
import threading

# poker_table.py lives under contracts/ (consensus bytecode builder + the slot/tag layout + read helpers this
# indexer reuses). Make it importable whether or not the caller already put contracts/ on the path.
_CONTRACTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contracts")
if _CONTRACTS not in sys.path:
    sys.path.insert(0, _CONTRACTS)
import poker_table as pt


# ------------------------------------------------------------------ disk cache helpers ----
def _cache_path(node, name="poker"):
    """Per-ledger cache file beside the ledger, namespaced by ledger filename (a regnet run can't clobber a
    mainnet node's cache), mirroring rest_stats._cache_path / token_index."""
    ledger_path = getattr(node, "ledger_path", None)
    if not ledger_path:
        return None
    base = os.path.basename(ledger_path) or "ledger.db"
    return os.path.join(os.path.dirname(ledger_path) or ".", "poker_%s-%s.json" % (name, base))


def _load(node, name="poker"):
    path = _cache_path(node, name)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _save(node, obj, name="poker"):
    path = _cache_path(node, name)
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception as e:
        try:
            node.logger.app_log.warning("poker stats cache save failed: %s" % e)
        except Exception:
            pass


def _load_cache(node, name="poker"):
    attr = "_poker_stats_cache"
    c = getattr(node, attr, None)
    if c is None:
        c = _load(node, name)
        if c is not None:
            setattr(node, attr, c)
    return c


# ------------------------------------------------------------------ readers ----
def node_reader(node):
    """A reader closure over the node's committed VM state. Reads ONLY contract storage — never the ledger.
    Returns reader(addr) -> {int_key:int_val} (empty dict if the contract is unknown / VM disabled)."""
    def _r(addr):
        vms = getattr(node, "vm_state", None)
        if vms is None:
            return {}
        try:
            if vms.get_code(addr) is None:
                return {}
            return {int(k): int(v) for k, v in vms.load_storage(addr).items()}
        except Exception:
            return {}
    return _r


def rest_reader(base_url, fetch=None):
    """A reader closure over a node's REST read path GET /api/vm/contract/<addr>. `base_url` e.g.
    'http://185.100.232.5:5659'. `fetch(url)->json-dict` is injectable (tests); defaults to urllib. Returns
    reader(addr) -> {int_key:int_val}. Reads contract STATE only — no ledger scan."""
    if fetch is None:
        def fetch(url):
            import urllib.request
            with urllib.request.urlopen(url, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
    base = base_url.rstrip("/")

    def _r(addr):
        try:
            doc = fetch("%s/api/vm/contract/%s" % (base, addr))
            out = {}
            for row in doc.get("storage", []):
                out[int(row["key"])] = int(row["value"])
            return out
        except Exception:
            return {}
    return _r


# ------------------------------------------------------------------ the pure fold ----
def _blank_account(addr_hex):
    return {"address": addr_hex,
            "hands_played": 0,   # distinct (table, hand_no) the account put chips into
            "hands_won": 0,      # hands where the account collected a payout > 0
            "net": 0,            # net chips = won − wagered (conserves to 0 across all accounts per hand)
            "wagered": 0,        # Σ contributions (chips put into pots)
            "won": 0,            # Σ payouts (chips collected)
            "biggest_pot": 0,    # largest pot total the account won a share of
            "tables": []}        # sorted list of table addresses seen


def _seat_addr_hex(storage, seat):
    """Reconstruct a seat's FULL 28-byte address (7 big-endian words) from TAG_ADDR storage [SEC-C1]."""
    b = b"".join(int(storage.get(pt.addr_word_key(seat, w), 0) & 0xFFFFFFFF).to_bytes(4, "big")
                 for w in range(7))
    return b.hex()


def _empty_state():
    """The incremental accumulator: per-account aggregates + per-table high-water (the dedupe/reorg anchor)."""
    return {"version": 1,
            "accounts": {},          # addr_hex -> account dict
            "tables": {}}            # table_addr -> {"resultcnt": int} (append-only high-water = dedupe anchor)


def _credit_results(state, reader, table_addr):
    """INCREMENTALLY fold a single table's new TAG_RESULT records into `state`. A PURE fold over the
    append-only log alone (one record per participating seat: hand_no, seat, payout, contribution) — it does
    NOT read mutable per-seat slots, so feeding the same canonical log always yields the same totals. Reads
    contract STATE only via `reader` (no ledger). Returns the number of new records folded.

    Incremental rule [SEC-M2]: read S_RESULTCNT; fold only records [cached_cnt .. cnt) — never a rescan. If
    S_RESULTCNT dropped below the cached value (a reorg rolled records back), this table is flagged for a
    full rebuild (the maintainer's cold path re-folds every table) so we never double-count."""
    storage = reader(table_addr)
    if not storage:
        return 0
    cnt = pt.result_count(storage)
    tinfo = state["tables"].setdefault(table_addr, {"resultcnt": 0})
    prev = int(tinfo.get("resultcnt", 0))

    if cnt < prev:
        state["_dirty"] = True       # reorg shrink -> caller does a full cold rebuild for exactness
        return 0
    if cnt == prev:
        return 0

    # group the NEW records by hand_no so we can credit hands_played once per (account, hand) and compute the
    # hand's pot total (= Σ payouts over the hand's records, == Σ contributions by the closing invariant).
    new_recs = [pt.read_result(storage, seq) for seq in range(prev, cnt)]
    pot_by_hand = {}
    for rec in new_recs:
        pot_by_hand[rec["hand_no"]] = pot_by_hand.get(rec["hand_no"], 0) + rec["payout"]

    for rec in new_recs:
        # key off the address EMBEDDED in the record (captured at settle), NOT the mutable current TAG_ADDR —
        # so a later seat reassignment (Stage 5 turnover) can never mis-credit this hand. [review #1 fix]
        addr_hex = rec["addr_hex"]
        acc = state["accounts"].setdefault(addr_hex, _blank_account(addr_hex))
        acc["hands_played"] += 1            # one record == one (account, hand) participation
        acc["wagered"] += rec["contrib"]
        acc["won"] += rec["payout"]
        if rec["payout"] > 0:
            acc["hands_won"] += 1
            pot = pot_by_hand.get(rec["hand_no"], 0)
            if pot > acc["biggest_pot"]:
                acc["biggest_pot"] = pot
        acc["net"] = acc["won"] - acc["wagered"]
        if table_addr not in acc["tables"]:
            acc["tables"].append(table_addr)
            acc["tables"].sort()

    tinfo["resultcnt"] = cnt
    return len(new_recs)


# ------------------------------------------------------------------ public build / maintain ----
def cold_build(reader, tables):
    """Full deterministic fold from seq 0 over every table address in `tables`. Pure (reorg-safe / idempotent):
    feeding the same canonical storage yields the same state. Reads contract STATE only."""
    state = _empty_state()
    for addr in sorted(tables):
        _credit_results(state, reader, addr)
    state.pop("_dirty", None)
    return state


def advance(state, reader, tables):
    """Incremental top-up: fold only NEW result records (>= each table's cached high-water). If a reorg shrank
    any table (sets _dirty), do a full cold rebuild for exactness. Returns the (possibly new) state."""
    for addr in sorted(tables):
        _credit_results(state, reader, addr)
    if state.pop("_dirty", False):
        return cold_build(reader, tables)
    return state


def leaderboard(state, metric="net", top=50):
    """Top-N accounts. metric in {'net','won','hands_won','hands_played','biggest_pot'}. Pure read over state."""
    key = metric if metric in ("net", "won", "hands_won", "hands_played", "biggest_pot") else "net"
    rows = sorted(state.get("accounts", {}).values(), key=lambda a: (a.get(key, 0), a["address"]),
                  reverse=True)
    return [_account_copy(r) for r in rows[:max(0, int(top))]]


def _account_copy(a):
    """Deep-enough copy: the `tables` list must not alias the cached state's list (caller may serialize/mutate)."""
    return {**a, "tables": list(a.get("tables", []))}


def account(state, addr_hex):
    """One account's stats keyed by FULL 28-byte address (hex). Returns a blank account if unseen."""
    addr_hex = addr_hex.lower()
    return _account_copy(state.get("accounts", {}).get(addr_hex, _blank_account(addr_hex)))


def table_results(reader, table_addr, since=0, limit=200):
    """The raw append-only result records for one table (for /api/poker/table/<addr>/results). Reads STATE
    only; returns records [since .. min(cnt, since+limit))."""
    storage = reader(table_addr)
    cnt = pt.result_count(storage)
    out = []
    for seq in range(max(0, int(since)), min(cnt, int(since) + int(limit))):
        out.append(pt.read_result(storage, seq))
    return {"table": table_addr, "resultcnt": cnt, "results": out}


# ------------------------------------------------------------------ node integration (request + daemon) ----
def _poker_tables(node):
    """Discover poker-table contract addresses from the VM state WITHOUT scanning the ledger: list deployed
    contracts and keep the ones whose S_MAGIC matches poker_table's sentinel. Returns []."""
    vms = getattr(node, "vm_state", None)
    if vms is None:
        return []
    try:
        addrs = list(vms.list_contracts())
    except Exception:
        return []
    magic = getattr(pt, "S_MAGIC_VALUE", None)
    out = []
    for a in addrs:
        try:
            slots = vms.load_storage(a)
            mv = int(slots.get(pt.S_MAGIC, 0))
            if magic is None or mv == magic:
                # accept any contract that has a result high-water slot populated, OR magic match
                if magic is None:
                    if pt.S_RESULTCNT in slots or pt.S_HANDNO in slots:
                        out.append(a)
                else:
                    out.append(a)
        except Exception:
            continue
    return out


_LOCK = threading.Lock()


def _maintain(node):
    """The ONLY place the (cheap, state-only) poker fold runs in the background. Single guarded daemon: cold
    build if no cache, else incremental advance. Reads contract STATE only — never the ledger."""
    if getattr(node, "_poker_maintaining", False):
        return
    node._poker_maintaining = True

    def work():
        try:
            reader = node_reader(node)
            tables = _poker_tables(node)
            cache = _load_cache(node) or None
            if cache is None or "accounts" not in cache:
                state = cold_build(reader, tables)
            else:
                state = advance(cache, reader, tables)
            state["tables_seen"] = tables
            setattr(node, "_poker_stats_cache", state)
            _save(node, state)
        except Exception as e:
            try:
                node.logger.app_log.warning("poker stats maintain failed: %s" % e)
            except Exception:
                pass
        finally:
            node._poker_maintaining = False

    t = threading.Thread(target=work, name="poker_stats_maintain", daemon=True)
    t.start()


def request_leaderboard(node, metric="net", top=50):
    """CHEAP request path (mirrors rest_stats): return the cached leaderboard immediately + kick the daemon.
    First call (no cache yet) returns status 'computing'."""
    cache = _load_cache(node)
    _maintain(node)
    if cache is None or "accounts" not in cache:
        return {"status": "computing", "metric": metric, "leaderboard": []}
    return {"status": "ok", "metric": metric, "top": int(top),
            "leaderboard": leaderboard(cache, metric=metric, top=top)}


def request_account(node, addr_hex):
    cache = _load_cache(node)
    _maintain(node)
    if cache is None or "accounts" not in cache:
        return {"status": "computing", "account": account(_empty_state(), addr_hex)}
    return {"status": "ok", "account": account(cache, addr_hex)}


def request_table_results(node, table_addr, since=0, limit=200):
    """Live read of one table's result log (state-only); no cache needed — it is cheap and always current."""
    return {"status": "ok", **table_results(node_reader(node), table_addr, since=since, limit=limit)}
