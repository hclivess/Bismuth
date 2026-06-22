"""Aggregate statistics for the explorer stats page (doc/15). Read-only and consensus-neutral.

A "top explorer" dashboard's worth of metrics, in three cost classes:
  * CHEAP, per-request: difficulty time-series (sampled from the indexed misc table) and the current-status
    numbers.
  * HEAVY, background-cached + INCREMENTAL: every full-ledger aggregate (monthly tx/volume/fees/emission/
    active-addresses, new addresses, rich list, top miners, largest transactions) is computed ONCE in a
    daemon thread, mirrored to a small JSON file beside the ledger, then topped up over only the new blocks
    per call — the pattern rest_api uses for circulating supply. The first call returns status "computing".
    The request path NEVER does heavy work: it returns the current cached snapshot immediately and kicks ONE
    guarded background maintainer (``_maintain``) that brings every cache up to tip — cold build if missing,
    incremental advance if stale — one stat at a time, then exits (never several multi-GB scans at once, and
    no API request can block on a scan, not even the first after a restart).
  * EXTERNAL, TTL-cached: node geolocation (ip-api.com) for the world map and market data (coingecko) for
    price / market cap, both fetched server-side (no https mixed-content), gated by rest_api_geo /
    rest_api_market (default on) and degrading to nothing on failure.
"""
import json
import os
import threading
import time
import urllib.request

import dbhandler

GEO_TTL = 6 * 3600          # re-geolocate peers at most every 6 hours
GEO_API = "http://ip-api.com/batch"   # free tier: http only, 100 IPs/batch, ~15 req/min — fine when cached
MARKET_TTL = 300            # refresh price at most every 5 minutes
MARKET_API = ("https://api.coingecko.com/api/v3/coins/bismuth?localization=false&tickers=false"
              "&market_data=true&community_data=false&developer_data=false&sparkline=false")


# ------------------------------------------------------------------ disk cache helpers ----
def _cache_path(node, name):
    """A per-ledger stats cache file beside the ledger (namespaced by ledger filename so a regnet run can't
    clobber a mainnet node's cache), mirroring rest_api.supply_cache_path."""
    ledger_path = getattr(node, "ledger_path", None)
    if not ledger_path:
        return None
    base = os.path.basename(ledger_path) or "ledger.db"
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


def _load_cache(node, name):
    """In-memory cache, falling back to the on-disk mirror (and memoizing it)."""
    attr = "_stats_%s_cache" % name
    c = getattr(node, attr, None)
    if c is None:
        c = _load(node, name)
        if c is not None:
            setattr(node, attr, c)
    return c


def _open_db(node):
    return dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                               node.ledger_ram_file, node.logger, trace_db_calls=node.trace_db_calls)


def _to_bis(raw):
    """A raw ledger amount/fee/reward SUM -> float BIS, honoring the storage mode (integer units vs decimal).
    Display only; lossy above 2**53 units, never used for consensus."""
    import amounts
    if amounts.LEDGER_INTEGER:
        return float(amounts.to_decimal(raw or 0))
    return float(raw or 0)


# ------------------------------------------------- self-driving heavy background compute ----
def _maintain(node):
    """The ONLY place heavy ledger work happens for stats. A single guarded background daemon brings every
    heavy stat cache up to the current tip — COLD build if missing, INCREMENTAL advance if stale — one stat
    at a time (the 23GB ledger is never hit by several scans at once), then exits. The request path NEVER
    does heavy work: it returns the current cached snapshot immediately (a few blocks stale at most) and just
    kicks this. So no API request can ever block on a scan — not even the first one after a restart, which
    used to run a multi-block catch-up on the request thread and trip nginx's read timeout."""
    if getattr(node, "_stats_maintaining", False):
        return
    node._stats_maintaining = True
    specs = [("monthly", _monthly_cold, _monthly_advance), ("newaddr", _newaddr_cold, _newaddr_advance),
             ("rich", _rich_cold, _rich_advance), ("miners", _miners_cold, _miners_advance),
             ("bigtx", _bigtx_cold, _bigtx_advance)]

    def work():
        try:
            d = _open_db(node)
            try:
                for name, cold_fn, adv_fn in specs:
                    attr = "_stats_%s_cache" % name
                    cache = getattr(node, attr, None) or _load(node, name)
                    tip = int(getattr(node, "hdd_block", 0) or 0)
                    try:
                        if cache is None:
                            cache = cold_fn(d, tip)
                            try:
                                node.logger.app_log.warning("Status: stats '%s' cache built at height %s" % (name, tip))
                            except Exception:
                                pass
                        elif tip > int(cache.get("height", 0)):
                            cache = adv_fn(d, cache, tip)
                        else:
                            setattr(node, attr, cache)       # at tip already -> just memoize
                            continue
                        setattr(node, attr, cache)
                        _save(node, name, cache)
                    except Exception as e:
                        try:
                            node.logger.app_log.warning("stats '%s' maintain failed: %s" % (name, e))
                        except Exception:
                            pass
            finally:
                d.close()
        finally:
            node._stats_maintaining = False

    threading.Thread(target=work, daemon=True).start()


# ===================================================================== monthly series ====
# One scan, many per-month metrics: non-coinbase tx count, value transferred, fees, coin emission (block
# subsidy), and distinct active senders. The additive four top up incrementally; active-senders (a COUNT
# DISTINCT, not additively mergeable) is the cold-scan value and is not advanced by the incremental top-up.
_MONTHLY_ADD_SQL = (
    "SELECT strftime('%Y-%m', timestamp, 'unixepoch') AS m, "
    "SUM(CASE WHEN reward = 0 THEN 1 ELSE 0 END) AS txs, "
    "SUM(amount) AS vol, SUM(fee) AS fees, SUM(reward) AS emission "
    "FROM transactions WHERE block_height > ? AND block_height <= ? GROUP BY m")
_MONTHLY_ACTIVE_SQL = (
    "SELECT strftime('%Y-%m', timestamp, 'unixepoch') AS m, COUNT(DISTINCT address) "
    "FROM transactions WHERE block_height > 0 GROUP BY m")


def _month_blank():
    return {"txs": 0, "vol": 0, "fees": 0, "emission": 0, "active": 0}


def _monthly_cold(d, tip):
    months = {}
    for (m, txs, vol, fees, em) in (d.fetchall(d.h, _MONTHLY_ADD_SQL, (0, tip)) or []):
        if not m:
            continue
        months[m] = {"txs": int(txs or 0), "vol": vol or 0, "fees": fees or 0,
                     "emission": em or 0, "active": 0}
    for (m, a) in (d.fetchall(d.h, _MONTHLY_ACTIVE_SQL, ()) or []):
        if not m:
            continue
        months.setdefault(m, _month_blank())["active"] = int(a or 0)
    return {"height": tip, "active_height": tip, "months": months}


def _monthly_advance(d, cache, tip):
    ch = int(cache.get("height", 0))
    months = {k: dict(v) for k, v in cache.get("months", {}).items()}
    for (m, txs, vol, fees, em) in (d.fetchall(d.h, _MONTHLY_ADD_SQL, (ch, tip)) or []):
        if not m:
            continue
        e = months.setdefault(m, _month_blank())
        e["txs"] += int(txs or 0); e["vol"] += (vol or 0)
        e["fees"] += (fees or 0); e["emission"] += (em or 0)
    return {"height": tip, "active_height": cache.get("active_height", ch), "months": months}


def monthly(node, db):
    """Per-month {month, txs, volume, fees, emission, issued (cumulative block reward), active}. status ok|computing.
    NB: `issued` is cumulative block-reward emission (coins minted), NOT exact circulating supply — it does not
    net burned fees or the dev/HN reward mirrors; /api/supply is the authoritative circulating figure."""
    cache = _load_cache(node, "monthly")
    if cache is None:
        _maintain(node)
        return {"status": "computing", "height": int(getattr(node, "hdd_block", 0) or 0), "months": []}
    if int(cache.get("height", 0)) < int(getattr(node, "hdd_block", 0) or 0):
        _maintain(node)                          # advance in the background; serve the current snapshot now
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
# An address is "created" the first time it RECEIVES at a positive height. Cold = MIN(timestamp)-per-recipient
# GROUP BY; incremental = only new recipients, each tested for a prior appearance via one indexed lookup.
_NEWADDR_COLD_SQL = (
    "SELECT strftime('%Y-%m', f, 'unixepoch') AS m, COUNT(*) FROM ("
    "  SELECT recipient AS r, MIN(timestamp) AS f FROM transactions WHERE block_height > 0 GROUP BY recipient"
    ") GROUP BY m")
_NEWADDR_RANGE_SQL = ("SELECT recipient, MIN(timestamp) FROM transactions "
                      "WHERE block_height > ? AND block_height <= ? GROUP BY recipient")
_NEWADDR_SEEN_SQL = ("SELECT 1 FROM transactions WHERE recipient = ? AND block_height > 0 "
                     "AND block_height <= ? LIMIT 1")


def _newaddr_cold(d, tip):
    months = {m: int(c) for (m, c) in (d.fetchall(d.h, _NEWADDR_COLD_SQL, ()) or []) if m}
    return {"height": tip, "months": months, "total": sum(months.values())}


def _newaddr_advance(d, cache, tip):
    ch = int(cache.get("height", 0))
    months = dict(cache.get("months", {}))
    for (recip, first_ts) in (d.fetchall(d.h, _NEWADDR_RANGE_SQL, (ch, tip)) or []):
        if not recip or first_ts is None:
            continue
        if d.fetchall(d.h, _NEWADDR_SEEN_SQL, (recip, ch)):
            continue                                    # appeared before the cached tip -> not new
        m = time.strftime("%Y-%m", time.gmtime(float(first_ts)))
        months[m] = months.get(m, 0) + 1
    return {"height": tip, "months": months, "total": sum(months.values())}


def new_addresses(node, db):
    """Cached monthly count of newly-created addresses. {status, height, total (cumulative), months:[{month,count}]}."""
    cache = _load_cache(node, "newaddr")
    if cache is None:
        _maintain(node)
        return {"status": "computing", "height": int(getattr(node, "hdd_block", 0) or 0), "months": []}
    if int(cache.get("height", 0)) < int(getattr(node, "hdd_block", 0) or 0):
        _maintain(node)
    ms = sorted(cache.get("months", {}).items())
    return {"status": "ok", "height": cache["height"], "total": cache.get("total", 0),
            "months": [{"month": m, "count": c} for m, c in ms]}


# ============================================================================ rich list ====
# Top addresses by balance (credit = SUM(amount+reward) as recipient, debit = SUM(amount+fee) as address,
# ALL heights — identical to essentials.ledger_balance3). Cold = one capped GROUP BY; incremental =
# recompute only TOUCHED addresses (indexed per-address aggregate) and re-merge into the cached top-K.
RICH_K = 500
_RICH_COLD_SQL = (
    "SELECT a, SUM(v) AS bal FROM ("
    "  SELECT recipient AS a, amount + reward AS v FROM transactions"
    "  UNION ALL"
    "  SELECT address AS a, -(amount + fee) AS v FROM transactions"
    ") GROUP BY a ORDER BY bal DESC LIMIT ?")
_RICH_ADDR_SQL = (
    "SELECT SUM(CASE WHEN recipient = ?1 THEN amount + reward ELSE 0 END), "
    "       SUM(CASE WHEN address = ?1 THEN amount + fee ELSE 0 END) "
    "FROM transactions WHERE recipient = ?1 OR address = ?1")
_RICH_TOUCHED_SQL = (
    "SELECT recipient AS a FROM transactions WHERE (block_height > ?1 AND block_height <= ?2) "
    "  OR (block_height < -?1 AND block_height >= -?2) "
    "UNION SELECT address AS a FROM transactions WHERE (block_height > ?1 AND block_height <= ?2) "
    "  OR (block_height < -?1 AND block_height >= -?2)")


def _rich_balance_bis(credit_raw, debit_raw):
    import amounts
    if amounts.LEDGER_INTEGER:
        return float(amounts.to_decimal(credit_raw or 0) - amounts.to_decimal(debit_raw or 0))
    return float((credit_raw or 0) - (debit_raw or 0))


def _rich_cold(d, tip):
    top = {}
    for (a, bal) in (d.fetchall(d.h, _RICH_COLD_SQL, (RICH_K,)) or []):
        if a:
            top[a] = _rich_balance_bis(bal, 0)          # cold `bal` is already credit-minus-debit (net)
    return {"height": tip, "top": top}


def _rich_advance(d, cache, tip):
    ch = int(cache.get("height", 0))
    balances = dict(cache.get("top", {}))
    touched = {a for (a,) in (d.fetchall(d.h, _RICH_TOUCHED_SQL, (ch, tip)) or []) if a}
    for a in touched:
        cr, dr = (d.fetchall(d.h, _RICH_ADDR_SQL, (a,)) or [(0, 0)])[0]
        bal = _rich_balance_bis(cr, dr)
        if bal > 0:
            balances[a] = bal
        else:
            balances.pop(a, None)
    ranked = sorted(balances.items(), key=lambda kv: -kv[1])[:RICH_K]
    return {"height": tip, "top": dict(ranked)}


def rich_list(node, db, top=100):
    """Cached top-balance address list. {status, height, count, rich:[{rank,address,balance}]}."""
    top = max(1, min(int(top or 100), RICH_K))
    cache = _load_cache(node, "rich")
    if cache is None:
        _maintain(node)
        return {"status": "computing", "height": int(getattr(node, "hdd_block", 0) or 0), "rich": []}
    if int(cache.get("height", 0)) < int(getattr(node, "hdd_block", 0) or 0):
        _maintain(node)
    ranked = sorted(cache.get("top", {}).items(), key=lambda kv: -kv[1])[:top]
    return {"status": "ok", "height": cache["height"], "count": len(ranked),
            "rich": [{"rank": i + 1, "address": a, "balance": round(b, 8)}
                     for i, (a, b) in enumerate(ranked)]}


# ========================================================================== top miners ====
# Mining distribution: blocks mined + reward earned per coinbase recipient (reward>0, positive height).
# Cold = GROUP BY recipient; incremental = additive over new blocks.
_MINERS_COLD_SQL = ("SELECT recipient, COUNT(*), SUM(reward) FROM transactions "
                    "WHERE block_height > 0 AND reward > 0 GROUP BY recipient")
_MINERS_RANGE_SQL = ("SELECT recipient, COUNT(*), SUM(reward) FROM transactions "
                     "WHERE block_height > ? AND block_height <= ? AND reward > 0 GROUP BY recipient")


def _miners_cold(d, tip):
    m = {}
    for (r, b, rw) in (d.fetchall(d.h, _MINERS_COLD_SQL, ()) or []):
        if r:
            m[r] = {"blocks": int(b or 0), "reward": rw or 0}
    return {"height": tip, "miners": m}


def _miners_advance(d, cache, tip):
    m = {k: dict(v) for k, v in cache.get("miners", {}).items()}
    for (r, b, rw) in (d.fetchall(d.h, _MINERS_RANGE_SQL, (int(cache.get("height", 0)), tip)) or []):
        if not r:
            continue
        e = m.setdefault(r, {"blocks": 0, "reward": 0})
        e["blocks"] += int(b or 0); e["reward"] += (rw or 0)
    return {"height": tip, "miners": m}


def top_miners(node, db, top=50):
    """Cached mining distribution. {status, height, miner_count, miners:[{rank,address,blocks,reward,share%}]}."""
    top = max(1, min(int(top or 50), 1000))
    cache = _load_cache(node, "miners")
    if cache is None:
        _maintain(node)
        return {"status": "computing", "height": int(getattr(node, "hdd_block", 0) or 0), "miners": []}
    if int(cache.get("height", 0)) < int(getattr(node, "hdd_block", 0) or 0):
        _maintain(node)
    miners = cache.get("miners", {})
    total_blocks = sum(v["blocks"] for v in miners.values()) or 1
    ranked = sorted(miners.items(), key=lambda kv: -kv[1]["blocks"])[:top]
    return {"status": "ok", "height": cache["height"], "miner_count": len(miners),
            "miners": [{"rank": i + 1, "address": a, "blocks": v["blocks"],
                        "reward": round(_to_bis(v["reward"]), 4),
                        "share": round(100.0 * v["blocks"] / total_blocks, 2)}
                       for i, (a, v) in enumerate(ranked)]}


# ===================================================================== largest transactions ====
BIGTX_K = 100
_BIGTX_COLD_SQL = ("SELECT block_height, timestamp, address, recipient, amount, signature FROM transactions "
                   "WHERE block_height > 0 AND reward = 0 ORDER BY amount DESC LIMIT ?")
# NB: the per-request range query must NOT "ORDER BY amount" — there is no index on amount, so combined with
# the height range SQLite falls back to a (near) full-table scan EVERY request (observed: 25s+ on mainnet).
# Filter by the indexed block_height range only (tiny: the new blocks since the cached tip) and let Python
# rank the merge below. The cold scan can afford the one-time amount-sort; the hot path must not.
_BIGTX_RANGE_SQL = ("SELECT block_height, timestamp, address, recipient, amount, signature FROM transactions "
                    "WHERE block_height > ? AND block_height <= ? AND reward = 0")


def _bigtx_row(r):
    return {"height": int(r[0]), "timestamp": float(r[1]) if r[1] is not None else None,
            "sender": r[2], "recipient": r[3], "amount": round(_to_bis(r[4]), 8),
            "signature": (r[5] or "")[:56]}


def _bigtx_cold(d, tip):
    return {"height": tip, "txs": [_bigtx_row(r) for r in (d.fetchall(d.h, _BIGTX_COLD_SQL, (BIGTX_K,)) or [])]}


def _bigtx_advance(d, cache, tip):
    new = [_bigtx_row(r) for r in (d.fetchall(d.h, _BIGTX_RANGE_SQL, (int(cache.get("height", 0)), tip)) or [])]
    seen, uniq = set(), []
    for t in sorted(cache.get("txs", []) + new, key=lambda x: -x["amount"]):
        k = t["signature"] or (t["height"], t["sender"], t["recipient"], t["amount"])
        if k in seen:
            continue
        seen.add(k); uniq.append(t)
        if len(uniq) >= BIGTX_K:
            break
    return {"height": tip, "txs": uniq}


def largest_txs(node, db, top=25):
    """Cached largest (by amount) non-coinbase transactions. {status, height, txs:[{height,timestamp,sender,
    recipient,amount,signature}]}. Incremental: only new blocks' top txs are merged into the cached top-K."""
    top = max(1, min(int(top or 25), BIGTX_K))
    cache = _load_cache(node, "bigtx")
    if cache is None:
        _maintain(node)
        return {"status": "computing", "height": int(getattr(node, "hdd_block", 0) or 0), "txs": []}
    if int(cache.get("height", 0)) < int(getattr(node, "hdd_block", 0) or 0):
        _maintain(node)
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
    """Sample the per-block difficulty from the indexed misc table at ~`points` evenly spaced heights, and
    attach each sampled height's block timestamp (one ranged query over the transactions index). Cheap —
    no full scan. Returns [{height, difficulty, timestamp}] ascending by height."""
    tip = int(tip or 0)
    if tip <= 0:
        return []
    step = max(1, tip // max(1, points))
    # Query the ~`points` explicit sample heights via the misc block_height index. NOT `block_height % step`
    # -- a modulo on the column can't use any index, so it full-scans the multi-million-row misc table every
    # call (~1.3s in isolation, and it gets I/O-starved + times out under concurrent page load -> the chart
    # intermittently fell back to "No difficulty history"). An IN list of indexed point lookups is ~ms.
    sample = list(range(step, tip + 1, step))
    if not sample:
        return []
    qmarks = ",".join("?" * len(sample))
    rows = db.fetchall(db.h, "SELECT block_height, difficulty FROM misc "
                             "WHERE block_height IN (%s) ORDER BY block_height ASC" % qmarks, tuple(sample))
    series = []
    heights = []
    for h, diff in (rows or []):
        try:
            series.append({"height": int(h), "difficulty": float(diff), "timestamp": None})
            heights.append(int(h))
        except (TypeError, ValueError):
            continue
    # attach a representative timestamp per sampled height (min ts at that height == the block's time)
    if heights:
        qmarks = ",".join("?" * len(heights))
        ts = db.fetchall(db.h, "SELECT block_height, MIN(timestamp) FROM transactions "
                               "WHERE block_height IN (%s) GROUP BY block_height" % qmarks, tuple(heights))
        tsm = {int(h): float(t) for (h, t) in (ts or []) if t is not None}
        for s in series:
            s["timestamp"] = tsm.get(s["height"])
    return series


# -------------------------------------------------------------------- network summary ----
def network_summary(node, db):
    """A compact dashboard payload: height, current difficulty, recent average block time + implied
    hashrate, peer/connection counts, mempool size, token count. Cheap (status + a tiny recent-headers
    scan); supply & total-tx come from their own cached endpoints."""
    tip = int(getattr(node, "hdd_block", 0) or 0)
    diff = node.difficulty[0] if getattr(node, "difficulty", None) else None
    # average block time over the most recent ~120 blocks, from the coinbase timestamps (one indexed scan)
    avg_block_time = None
    try:
        lo = max(1, tip - 120)
        rows = db.fetchall(db.h, "SELECT block_height, MIN(timestamp) FROM transactions "
                                 "WHERE block_height >= ? AND block_height <= ? GROUP BY block_height "
                                 "ORDER BY block_height ASC", (lo, tip))
        tss = [float(t) for (_h, t) in (rows or []) if t is not None]
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
        mempool_count = len(mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND)) if mp.MEMPOOL else 0
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
def _peer_ips(node):
    """Distinct bare-host peer IPs the node knows (connection pool + opinions), capped."""
    p = getattr(node, "peers", None)
    if not p:
        return []
    ips = set()
    for ip in (getattr(p, "peer_opinion_dict", {}) or {}):
        ips.add(str(ip).split(":")[0])
    for c in (getattr(p, "_connection_pool_set", set()) or set()):
        ips.add(str(c).split(":")[0])
    # only public IPv4-ish entries are geolocatable; drop obvious non-routables cheaply
    out = [ip for ip in ips if ip and not ip.startswith(("127.", "10.", "192.168.", "169.254.", "0."))]
    return sorted(out)[:300]


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
            ips = _peer_ips(node)
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
    Returns {status, points:[{ip,lat,lon,country,cc,city}], countries:[{country,cc,count}]}. The first call
    (cold) kicks the background lookup and returns status 'computing'."""
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
    points, by_country = [], {}
    for ip, g in geo.items():
        if g.get("lat") is None or g.get("lon") is None:
            continue
        points.append({"ip": ip, "lat": g["lat"], "lon": g["lon"],
                       "country": g.get("country"), "cc": g.get("cc"), "city": g.get("city")})
        key = (g.get("cc") or "??", g.get("country") or "Unknown")
        by_country[key] = by_country.get(key, 0) + 1
    countries = [{"cc": cc, "country": c, "count": n}
                 for (cc, c), n in sorted(by_country.items(), key=lambda kv: -kv[1])]
    return {"status": "ok", "ts": cache.get("ts"), "count": len(points),
            "points": points, "countries": countries}
