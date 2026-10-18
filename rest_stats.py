"""Aggregate statistics for the explorer stats page (doc/15). Read-only and consensus-neutral.

A "top explorer" dashboard's worth of metrics, in three cost classes:
  * CHEAP, per-request: difficulty time-series (point reads of the ledger's difficulty index at sampled
    heights) and the current-status numbers.
  * HEAVY, background-cached + INCREMENTAL: every full-ledger aggregate (monthly tx/volume/fees/emission/
    active-addresses, new addresses, top miners, largest transactions) is built by ONE ascending block scan
    of the LMDB ledger in a daemon thread, mirrored to a small JSON file beside the ledger, then topped up
    over only the new blocks — the pattern rest_api uses for circulating supply. The rich list reads the
    ledger's maintained per-address balances directly. The first call returns status "computing". The
    request path NEVER does heavy work: it returns the current cached snapshot immediately and kicks ONE
    guarded background maintainer (``_maintain``) that brings every cache up to tip in a single shared pass
    (cold build if missing, incremental advance if stale), then exits — no API request can block on a scan.
  * EXTERNAL, TTL-cached: node geolocation (ip-api.com) for the world map and market data (coingecko) for
    price / market cap, both fetched server-side (no https mixed-content), gated by rest_api_geo /
    rest_api_market (default on) and degrading to nothing on failure.

All heavy-cache amounts are kept in integer atomic units (storage-mode agnostic) and converted for display.
"""
import heapq
import json
import os
import threading
import time
import urllib.request

import amounts
import block_store

GEO_TTL = 6 * 3600          # re-geolocate peers at most every 6 hours
GEO_API = "http://ip-api.com/batch"   # free tier: http only, 100 IPs/batch, ~15 req/min — fine when cached
MARKET_TTL = 300            # refresh price at most every 5 minutes
MARKET_API = ("https://api.coingecko.com/api/v3/coins/bismuth?localization=false&tickers=false"
              "&market_data=true&community_data=false&developer_data=false&sparkline=false")

CACHE_VERSION = 2           # heavy caches built from the LMDB ledger in integer units; older files are rebuilt
SCAN_CHUNK = 2000           # blocks per read transaction during a scan (keeps LMDB read txns short)

_B = block_store


# ------------------------------------------------------------------ disk cache helpers ----
def _ledger_file(node):
    """The ledger path the per-ledger cache files are namespaced by."""
    path = getattr(node, "ledger_path", None)
    if not path:
        path = getattr(getattr(node, "ledger", None), "path", None)
    return path


def _cache_path(node, name):
    """A per-ledger stats cache file beside the ledger (namespaced by ledger filename so a regnet run can't
    clobber a mainnet node's cache), mirroring rest_api.supply_cache_path."""
    ledger_path = _ledger_file(node)
    if not ledger_path:
        return None
    ledger_path = str(ledger_path).rstrip("/")
    base = os.path.basename(ledger_path) or "ledger"
    return os.path.join(os.path.dirname(ledger_path) or ".", "stats_%s-%s.json" % (name, base))


def _load(node, name):
    path = _cache_path(node, name)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _save(node, name, obj):
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
            node.logger.app_log.warning("stats cache save failed (%s): %s" % (name, e))
        except Exception:
            pass


def _load_heavy(node, name):
    """A heavy-stat cache from disk, only when it was built by this cache version."""
    c = _load(node, name)
    if c is None or c.get("v") != CACHE_VERSION:
        return None
    return c


def _load_cache(node, name):
    """In-memory cache, falling back to the on-disk mirror (and memoizing it)."""
    attr = "_stats_%s_cache" % name
    c = getattr(node, attr, None)
    if c is None:
        c = _load_heavy(node, name)
        if c is not None:
            setattr(node, attr, c)
    return c


def _ledger(node, db=None):
    """The BlockStore: from a DbHandler when handed one, else the node's ledger."""
    store = getattr(db, "store", None) if db is not None else None
    return store if store is not None else node.ledger


def _to_bis(u):
    """Integer atomic units -> float BIS. Display only; lossy above 2**53 units, never used for consensus."""
    return float(amounts.to_decimal(int(u or 0)))


def _month(ts):
    return time.strftime("%Y-%m", time.gmtime(float(ts)))


def _scan(store, lo, hi, chunk=SCAN_CHUNK):
    """Yield ``(height, typed_rows)`` for [lo, hi] ascending, one short read transaction per chunk."""
    h = max(1, int(lo))
    hi = int(hi)
    while h <= hi:
        end = min(hi, h + chunk - 1)
        for item in list(store.blocks_in_range(h, end)):
            yield item
        h = end + 1


# ------------------------------------------------- self-driving heavy background compute ----
# Each block-scanned stat is a small accumulator class: ``start(cache)`` begins a cold build (cache None) or
# an incremental advance, ``feed(height, rows)`` folds one block, ``finish(tip)`` returns the new cache.
class _Monthly:
    """One scan, many per-month metrics: non-coinbase tx count, value transferred, fees, coin emission
    (block subsidy), and distinct active senders. The additive four top up incrementally; active-senders
    (a distinct count, not additively mergeable) is the cold-scan value and is not advanced."""
    name = "monthly"

    def start(self, store, cache):
        self.cold = cache is None
        self.cache = cache
        self.months = {} if self.cold else {k: dict(v) for k, v in cache.get("months", {}).items()}
        self.active = {}

    def feed(self, h, rows):
        for r in rows:
            m = _month(r[_B.TS])
            e = self.months.setdefault(m, _month_blank())
            if _B.units(r[_B.REWARD]) == 0:
                e["txs"] += 1
            e["vol"] += _B.units(r[_B.AMOUNT])
            e["fees"] += _B.units(r[_B.FEE])
            e["emission"] += _B.units(r[_B.REWARD])
            if self.cold:
                self.active.setdefault(m, set()).add(r[_B.ADDR])

    def finish(self, tip):
        if self.cold:
            for m, s in self.active.items():
                self.months.setdefault(m, _month_blank())["active"] = len(s)
            active_height = tip
        else:
            active_height = self.cache.get("active_height", self.cache.get("height", 0))
        return {"v": CACHE_VERSION, "height": tip, "active_height": active_height, "months": self.months}


class _NewAddresses:
    """An address is "created" the first time it RECEIVES at a positive height. Cold: the first sighting in
    the ascending scan. Incremental: a recipient first seen in the new blocks is tested for a prior
    appearance with one ordered read of the ledger's recipient index."""
    name = "newaddr"

    def start(self, store, cache):
        self.store = store
        self.cold = cache is None
        self.since = 0 if self.cold else int(cache.get("height", 0))
        self.months = {} if self.cold else dict(cache.get("months", {}))
        self.checked = set()

    def _first_recipient_height(self, addr):
        rows = self.store.address_rows(addr, role="recipient", reverse=False, limit=1, lo=1)
        return int(rows[0][_B.H]) if rows else None

    def feed(self, h, rows):
        for r in rows:
            recip = r[_B.RECIP]
            if not recip or recip in self.checked:
                continue
            self.checked.add(recip)
            if not self.cold:
                first = self._first_recipient_height(recip)
                if first is not None and first <= self.since:
                    continue                            # appeared before the cached tip -> not new
            m = _month(r[_B.TS])
            self.months[m] = self.months.get(m, 0) + 1

    def finish(self, tip):
        return {"v": CACHE_VERSION, "height": tip, "months": self.months, "total": sum(self.months.values())}


class _Miners:
    """Mining distribution: blocks mined + reward earned per coinbase recipient (reward>0, positive height)."""
    name = "miners"

    def start(self, store, cache):
        self.miners = {} if cache is None else {k: dict(v) for k, v in cache.get("miners", {}).items()}

    def feed(self, h, rows):
        for r in rows:
            rw = _B.units(r[_B.REWARD])
            if rw > 0 and r[_B.RECIP]:
                e = self.miners.setdefault(r[_B.RECIP], {"blocks": 0, "reward": 0})
                e["blocks"] += 1
                e["reward"] += rw

    def finish(self, tip):
        return {"v": CACHE_VERSION, "height": tip, "miners": self.miners}


BIGTX_K = 100


def _bigtx_row(r):
    return {"height": int(r[_B.H]), "timestamp": float(r[_B.TS]) if r[_B.TS] is not None else None,
            "sender": r[_B.ADDR], "recipient": r[_B.RECIP], "amount": round(_to_bis(_B.units(r[_B.AMOUNT])), 8),
            "signature": (r[_B.SIG] or "")[:56]}


class _BigTxs:
    """Largest non-coinbase transactions by amount: a bounded min-heap over the scanned rows, merged into
    the cached top-K."""
    name = "bigtx"

    def start(self, store, cache):
        self.prev = [] if cache is None else list(cache.get("txs", []))
        self.heap = []                                  # (amount units, seq, row)
        self.seq = 0

    def feed(self, h, rows):
        for r in rows:
            if _B.units(r[_B.REWARD]) != 0:
                continue
            item = (_B.units(r[_B.AMOUNT]), self.seq, r)
            self.seq += 1
            if len(self.heap) < BIGTX_K:
                heapq.heappush(self.heap, item)
            elif item[0] > self.heap[0][0]:
                heapq.heapreplace(self.heap, item)

    def finish(self, tip):
        new = [_bigtx_row(r) for _a, _s, r in self.heap]
        seen, uniq = set(), []
        for t in sorted(self.prev + new, key=lambda x: -x["amount"]):
            k = t["signature"] or (t["height"], t["sender"], t["recipient"], t["amount"])
            if k in seen:
                continue
            seen.add(k); uniq.append(t)
            if len(uniq) >= BIGTX_K:
                break
        return {"v": CACHE_VERSION, "height": tip, "txs": uniq}


def _month_blank():
    return {"txs": 0, "vol": 0, "fees": 0, "emission": 0, "active": 0}


def _maintain(node):
    """The ONLY place heavy ledger work happens for stats. A single guarded background daemon brings every
    heavy stat cache up to the current tip in ONE shared ascending pass over the blocks the stalest cache is
    missing — each accumulator only folds the blocks above its own cached height (cold build from block 1 if
    missing) — then refreshes the rich list from the maintained balances, and exits. The request path NEVER
    does heavy work: it returns the current cached snapshot immediately (a few blocks stale at most) and just
    kicks this."""
    if getattr(node, "_stats_maintaining", False):
        return
    node._stats_maintaining = True

    def work():
        try:
            store = node.ledger
            tip = int(store.tip() or 0)
            pending = []
            for acc in (_Monthly(), _NewAddresses(), _Miners(), _BigTxs()):
                attr = "_stats_%s_cache" % acc.name
                cache = getattr(node, attr, None) or _load_heavy(node, acc.name)
                height = int(cache.get("height", 0)) if cache is not None else 0
                if cache is not None and height >= tip:
                    setattr(node, attr, cache)           # at tip already -> just memoize
                    continue
                acc.start(store, cache)
                pending.append((acc, height, cache is None))
            if pending:
                lo = min(height for _a, height, _c in pending) + 1
                try:
                    for h, rows in _scan(store, lo, tip):
                        for acc, height, _c in pending:
                            if h > height:
                                acc.feed(h, rows)
                    for acc, _height, cold in pending:
                        cache = acc.finish(tip)
                        setattr(node, "_stats_%s_cache" % acc.name, cache)
                        _save(node, acc.name, cache)
                        if cold:
                            try:
                                node.logger.app_log.warning(
                                    "Status: stats '%s' cache built at height %s" % (acc.name, tip))
                            except Exception:
                                pass
                except Exception as e:
                    try:
                        node.logger.app_log.warning("stats maintain failed: %s" % e)
                    except Exception:
                        pass
            try:
                rich = getattr(node, "_stats_rich_cache", None) or _load_heavy(node, "rich")
                if rich is None:
                    rich = _rich_cold(store)
                elif int(rich.get("height", 0)) < tip:
                    rich = _rich_advance(store, rich, tip)
                setattr(node, "_stats_rich_cache", rich)
                _save(node, "rich", rich)
            except Exception as e:
                try:
                    node.logger.app_log.warning("stats 'rich' maintain failed: %s" % e)
                except Exception:
                    pass
        finally:
            node._stats_maintaining = False

    threading.Thread(target=work, daemon=True).start()


def _serve(node, name):
    """The cached snapshot for ``name`` (None while computing), kicking the maintainer when stale/missing."""
    cache = _load_cache(node, name)
    if cache is None or int(cache.get("height", 0)) < int(getattr(node, "hdd_block", 0) or 0):
        _maintain(node)                          # build/advance in the background; serve the current snapshot
    return cache


# ===================================================================== monthly series ====
def monthly(node, db):
    """Per-month {month, txs, volume, fees, emission, issued (cumulative block reward), active}. status ok|computing.
    NB: `issued` is cumulative block-reward emission (coins minted), NOT exact circulating supply — it does not
    net burned fees; /api/supply is the authoritative circulating figure."""
    cache = _serve(node, "monthly")
    if cache is None:
        return {"status": "computing", "height": int(getattr(node, "hdd_block", 0) or 0), "months": []}
    out, cum = [], 0.0
    for m in sorted(cache.get("months", {})):
        e = cache["months"][m]
        em = _to_bis(e.get("emission", 0)); cum += em
        out.append({"month": m, "txs": int(e.get("txs", 0)),
                    "volume": round(_to_bis(e.get("vol", 0)), 4), "fees": round(_to_bis(e.get("fees", 0)), 8),
                    "emission": round(em, 4), "issued": round(cum, 4), "active": int(e.get("active", 0))})
    totals = {"txs": sum(x["txs"] for x in out), "volume": round(sum(x["volume"] for x in out), 4),
              "fees": round(sum(x["fees"] for x in out), 8), "emission": round(cum, 4)}
    return {"status": "ok", "height": cache["height"], "active_height": cache.get("active_height"),
            "totals": totals, "months": out}


def tx_per_month(node, db):
    """Back-compat: the transactions-per-month histogram, now served from the unified monthly cache."""
    m = monthly(node, db)
    if m.get("status") != "ok":
        return {"status": m.get("status", "computing"), "height": m.get("height"), "months": []}
    return {"status": "ok", "height": m["height"], "total": m["totals"]["txs"],
            "months": [{"month": x["month"], "count": x["txs"]} for x in m["months"]]}


# ================================================================ new addresses / month ====
def new_addresses(node, db):
    """Cached monthly count of newly-created addresses. {status, height, total (cumulative), months:[{month,count}]}."""
    cache = _serve(node, "newaddr")
    if cache is None:
        return {"status": "computing", "height": int(getattr(node, "hdd_block", 0) or 0), "months": []}
    ms = sorted(cache.get("months", {}).items())
    return {"status": "ok", "height": cache["height"], "total": cache.get("total", 0),
            "months": [{"month": m, "count": c} for m, c in ms]}


# ============================================================================ rich list ====
# Top addresses by balance, read from the ledger's maintained per-address balance parts (credited amount +
# rewards - debited amount - fees — the consensus balance). Cold = one pass over the balance table keeping
# the top-K; incremental = re-read only the addresses TOUCHED by the new blocks and re-merge.
RICH_K = 500


def _rich_cold(store):
    tip = int(store.tip() or 0)
    top = heapq.nlargest(RICH_K, ((sum(p[:2]) - sum(p[2:]), a) for a, p in store.iter_balances()
                                  if a and sum(p[:2]) - sum(p[2:]) > 0))
    return {"v": CACHE_VERSION, "height": tip, "top": {a: _to_bis(u) for u, a in top}}


def _rich_advance(store, cache, tip):
    ch = int(cache.get("height", 0))
    balances = dict(cache.get("top", {}))
    touched = set()
    for _h, rows in _scan(store, ch + 1, tip):
        for r in rows:
            touched.add(r[_B.ADDR]); touched.add(r[_B.RECIP])
    for a in touched:
        if not a:
            continue
        u = store.balance_units(a)
        if u > 0:
            balances[a] = _to_bis(u)
        else:
            balances.pop(a, None)
    ranked = sorted(balances.items(), key=lambda kv: -kv[1])[:RICH_K]
    return {"v": CACHE_VERSION, "height": tip, "top": dict(ranked)}


def rich_list(node, db, top=100):
    """Cached top-balance address list. {status, height, count, rich:[{rank,address,balance}]}."""
    top = max(1, min(int(top or 100), RICH_K))
    cache = _serve(node, "rich")
    if cache is None:
        return {"status": "computing", "height": int(getattr(node, "hdd_block", 0) or 0), "rich": []}
    ranked = sorted(cache.get("top", {}).items(), key=lambda kv: -kv[1])[:top]
    return {"status": "ok", "height": cache["height"], "count": len(ranked),
            "rich": [{"rank": i + 1, "address": a, "balance": round(b, 8)}
                     for i, (a, b) in enumerate(ranked)]}


# ========================================================================== top miners ====
def top_miners(node, db, top=50):
    """Cached mining distribution. {status, height, miner_count, miners:[{rank,address,blocks,reward,share%}]}."""
    top = max(1, min(int(top or 50), 1000))
    cache = _serve(node, "miners")
    if cache is None:
        return {"status": "computing", "height": int(getattr(node, "hdd_block", 0) or 0), "miners": []}
    miners = cache.get("miners", {})
    total_blocks = sum(v["blocks"] for v in miners.values()) or 1
    ranked = sorted(miners.items(), key=lambda kv: -kv[1]["blocks"])[:top]
    return {"status": "ok", "height": cache["height"], "miner_count": len(miners),
            "miners": [{"rank": i + 1, "address": a, "blocks": v["blocks"],
                        "reward": round(_to_bis(v["reward"]), 4),
                        "share": round(100.0 * v["blocks"] / total_blocks, 2)}
                       for i, (a, v) in enumerate(ranked)]}


# ===================================================================== largest transactions ====
def largest_txs(node, db, top=25):
    """Cached largest (by amount) non-coinbase transactions. {status, height, txs:[{height,timestamp,sender,
    recipient,amount,signature}]}. Incremental: only new blocks' top txs are merged into the cached top-K."""
    top = max(1, min(int(top or 25), BIGTX_K))
    cache = _serve(node, "bigtx")
    if cache is None:
        return {"status": "computing", "height": int(getattr(node, "hdd_block", 0) or 0), "txs": []}
    return {"status": "ok", "height": cache["height"], "txs": cache.get("txs", [])[:top]}


# ============================================================ market / price (external) ====
def _market_compute(node):
    if getattr(node, "_stats_market_computing", False):
        return
    node._stats_market_computing = True

    def work():
        try:
            req = urllib.request.Request(MARKET_API, headers={"User-Agent": "bismuth-node"})
            with urllib.request.urlopen(req, timeout=12) as r:
                j = json.loads(r.read().decode("utf-8"))
            md = j.get("market_data", {}) or {}
            def g(d, k):
                return (md.get(d) or {}).get(k)
            data = {"price_usd": g("current_price", "usd"), "price_btc": g("current_price", "btc"),
                    "market_cap_usd": g("market_cap", "usd"), "volume_24h_usd": g("total_volume", "usd"),
                    "change_24h": md.get("price_change_percentage_24h"),
                    "circulating_supply": md.get("circulating_supply"), "ath_usd": g("ath", "usd")}
            cache = {"ts": int(time.time()), "data": data}
            node._stats_market_cache = cache
            _save(node, "market", cache)
        except Exception as e:
            try:
                node.logger.app_log.warning("market compute failed: %s" % e)
            except Exception:
                pass
        finally:
            node._stats_market_computing = False

    threading.Thread(target=work, daemon=True).start()


def market(node):
    """Best-effort price / market cap from coingecko (id 'bismuth'), TTL-cached server-side. Gated by
    rest_api_market (default on). {status, ts, price_usd, price_btc, market_cap_usd, volume_24h_usd,
    change_24h, circulating_supply, ath_usd}."""
    if not getattr(node, "rest_api_market", True):
        return {"status": "disabled"}
    cache = getattr(node, "_stats_market_cache", None)
    if cache is None:
        cache = _load(node, "market")
        if cache is not None:
            node._stats_market_cache = cache
    fresh = cache is not None and (int(time.time()) - int(cache.get("ts", 0)) < MARKET_TTL)
    if not fresh:
        _market_compute(node)               # refresh in the background; serve stale meanwhile
    if cache is None:
        return {"status": "computing"}
    out = {"status": "ok", "ts": cache.get("ts")}
    out.update(cache.get("data", {}))
    return out


# ------------------------------------------------------------ difficulty series (cheap) ----
def difficulty_series(db, tip, points=180):
    """Sample the per-block difficulty at ~`points` evenly spaced heights (point reads of the ledger's
    difficulty index) and attach each sampled block's timestamp (its coinbase time). Cheap — no scan.
    Returns [{height, difficulty, timestamp}] ascending by height."""
    tip = int(tip or 0)
    if tip <= 0:
        return []
    store = block_store.as_ledger(db)
    step = max(1, tip // max(1, points))
    sample = list(range(step, tip + 1, step))
    if not sample:
        return []
    diffs = store.difficulties(sample)
    series = []
    for h in sorted(diffs):
        try:
            series.append({"height": int(h), "difficulty": float(diffs[h]), "timestamp": None})
        except (TypeError, ValueError):
            continue
    for s in series:
        cb = store.coinbase(s["height"])
        if cb is not None and cb[_B.TS] is not None:
            try:
                s["timestamp"] = float(cb[_B.TS])
            except (TypeError, ValueError):
                pass
    return series


# -------------------------------------------------------------------- network summary ----
def network_summary(node, db):
    """A compact dashboard payload: height, current difficulty, recent average block time + implied
    hashrate, peer/connection counts, mempool size, token count. Cheap (status + a tiny recent-blocks
    read); supply & total-tx come from their own cached endpoints."""
    tip = int(getattr(node, "hdd_block", 0) or 0)
    diff = node.difficulty[0] if getattr(node, "difficulty", None) else None
    # average block time over the most recent ~120 blocks, from each block's earliest timestamp
    avg_block_time = None
    try:
        lo = max(1, tip - 120)
        tss = []
        for _h, rows in _ledger(node, db).blocks_in_range(lo, tip):
            ts = [float(r[_B.TS]) for r in rows if r[_B.TS] is not None]
            if ts:
                tss.append(min(ts))
        if len(tss) >= 2:
            span = tss[-1] - tss[0]
            avg_block_time = round(span / (len(tss) - 1), 2) if span > 0 else None
    except Exception:
        pass
    peers = node.peers if getattr(node, "peers", None) else None
    token_count = None
    try:
        if getattr(node, "token_index", None) is not None:
            token_count = len(node.token_index.tokens_list(100000))
    except Exception:
        token_count = None
    import mempool as mp
    mempool_count = 0
    try:
        mempool_count = mp.MEMPOOL.count() if mp.MEMPOOL else 0
    except Exception:
        mempool_count = 0
    return {
        "height": tip,
        "difficulty": diff,
        "avg_block_time": avg_block_time,
        "connections": len(peers.connection_pool) if peers else 0,
        "consensus": getattr(peers, "consensus", None) if peers else None,
        "consensus_percentage": getattr(peers, "consensus_percentage", None) if peers else None,
        "mempool": mempool_count,
        "tokens": token_count,
        "node_version": getattr(node, "app_version", None),
    }


# ------------------------------------------------------------------- node geolocation ----
def _peer_status(node):
    """Every peer IP the node knows -> 'connected' | 'banned' | 'known'. Includes the FULL set, not just
    live connections: the banlist (config bans + reputation bans, peers_reputation appends to it) and the
    known-peer set (peer_dict from peers.txt / suggested_peers.txt), so the map paints every peer the node
    is aware of. Non-routable / local IPs are dropped (not geolocatable). Capped."""
    p = getattr(node, "peers", None)
    if not p:
        return {}

    def host(x):
        return str(x).split(":")[0]

    # Snapshot every live peer collection under the peers lock, then iterate the copies — a worker/
    # consensus thread resizing any of these mid-iteration would otherwise raise "changed size during
    # iteration" and 500 the stats/geo endpoint.
    _lock = getattr(p, "peers_lock", None)
    def _snap():
        return (list(getattr(p, "peer_opinion_dict", {}) or {}),
                list(getattr(p, "_connection_pool_set", set()) or set()),
                list(getattr(p, "connection_pool", []) or []),
                list(getattr(p, "banlist", []) or []),
                list(getattr(p, "peer_dict", {}) or {}))
    if _lock is not None:
        with _lock:
            _opinion, _pool_set, _pool, _ban, _known = _snap()
    else:
        _opinion, _pool_set, _pool, _ban, _known = _snap()

    connected, banned, known = set(), set(), set()
    for ip in _opinion:
        connected.add(host(ip))
    for c in _pool_set:
        connected.add(host(c))
    for c in _pool:
        connected.add(host(c))
    for ip in _ban:
        banned.add(host(ip))
    for ip in _known:
        known.add(host(ip))
    status = {}
    for ip in (known | connected | banned):
        if not ip or ip.startswith(("127.", "10.", "192.168.", "169.254.", "0.")):
            continue
        # precedence: banned (even if also in peer_dict) > connected > merely known
        status[ip] = "banned" if ip in banned else ("connected" if ip in connected else "known")
    return dict(sorted(status.items())[:500])


def _geo_fetch(ips):
    """Batch-geolocate up to 100 IPs per ip-api.com call. Returns {ip: {country, countryCode, lat, lon,
    city}}. Best-effort: any failure yields {} for that batch."""
    out = {}
    for i in range(0, len(ips), 100):
        batch = ips[i:i + 100]
        body = json.dumps([{"query": ip, "fields": "query,status,country,countryCode,lat,lon,city"}
                           for ip in batch]).encode("utf-8")
        try:
            req = urllib.request.Request(GEO_API, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                arr = json.loads(r.read().decode("utf-8"))
            for rec in arr:
                if rec.get("status") == "success" and rec.get("query"):
                    out[rec["query"]] = {"country": rec.get("country"), "cc": rec.get("countryCode"),
                                         "lat": rec.get("lat"), "lon": rec.get("lon"), "city": rec.get("city")}
        except Exception:
            break               # rate-limited / offline: stop, use what we have
        time.sleep(1.0)         # stay well under the free-tier rate limit
    return out


def _geo_compute(node):
    if getattr(node, "_stats_geo_computing", False):
        return
    node._stats_geo_computing = True

    def work():
        try:
            ips = list(_peer_status(node).keys())   # geolocate the FULL set (connected + known + banned)
            geo = _geo_fetch(ips) if ips else {}
            cache = {"ts": int(time.time()), "geo": geo}
            node._stats_geo_cache = cache
            _save(node, "geo", cache)
        except Exception as e:
            try:
                node.logger.app_log.warning("geo compute failed: %s" % e)
            except Exception:
                pass
        finally:
            node._stats_geo_computing = False

    threading.Thread(target=work, daemon=True).start()


def geo_nodes(node):
    """Geolocated peers for the world map. Best-effort + cached with a TTL; gated by ``rest_api_geo``.
    Returns {status, points:[{ip,lat,lon,country,cc,city,status}], status_counts, countries}. Geolocation is
    cached (it doesn't change); each peer's status (connected|banned|known) is recomputed fresh per call.
    The first call (cold) kicks the background lookup and returns status 'computing'."""
    if not getattr(node, "rest_api_geo", True):
        return {"status": "disabled", "points": [], "countries": []}
    cache = getattr(node, "_stats_geo_cache", None)
    if cache is None:
        cache = _load(node, "geo")
        if cache is not None:
            node._stats_geo_cache = cache
    fresh = cache is not None and (int(time.time()) - int(cache.get("ts", 0)) < GEO_TTL)
    if not fresh:
        _geo_compute(node)          # refresh in the background; serve stale meanwhile if we have it
    if cache is None:
        return {"status": "computing", "points": [], "countries": []}
    geo = cache.get("geo", {}) or {}
    status_map = _peer_status(node)              # CURRENT status per ip (cheap; geolocation stays cached)
    points, by_country = [], {}
    counts = {"connected": 0, "known": 0, "banned": 0}
    for ip, g in geo.items():
        if g.get("lat") is None or g.get("lon") is None:
            continue
        st = status_map.get(ip, "known")         # geolocated earlier but no longer in any set -> treat as known
        points.append({"ip": ip, "lat": g["lat"], "lon": g["lon"], "country": g.get("country"),
                       "cc": g.get("cc"), "city": g.get("city"), "status": st})
        counts[st] = counts.get(st, 0) + 1
        key = (g.get("cc") or "??", g.get("country") or "Unknown")
        by_country[key] = by_country.get(key, 0) + 1
    countries = [{"cc": cc, "country": c, "count": n}
                 for (cc, c), n in sorted(by_country.items(), key=lambda kv: -kv[1])]
    return {"status": "ok", "ts": cache.get("ts"), "count": len(points),
            "status_counts": counts, "points": points, "countries": countries}
