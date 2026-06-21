"""Aggregate statistics for the explorer stats page (doc/15). Read-only and consensus-neutral.

Two cost classes:
  * CHEAP, per-request: a difficulty time-series sampled from the indexed ``misc(block_height, difficulty)``
    table, and the current-status numbers — computed on demand.
  * HEAVY, background-cached: the transactions-per-month histogram is a full-ledger GROUP BY (multi-minute
    on a multi-GB mainnet ledger), so it is computed in a daemon thread, cached on the node, mirrored to a
    small JSON file beside the ledger, and topped up INCREMENTALLY as the tip advances — exactly the
    pattern ``rest_api`` uses for circulating supply. The first call returns ``status: "computing"``.

Node geolocation for the world map is fetched best-effort from the public ip-api.com batch endpoint
(server-side, so the https explorer has no mixed-content problem), cached on the node with a TTL and
mirrored to disk. It is gated by ``rest_api_geo`` (default on); a node that does not want the outbound
lookup sets ``rest_api_geo=False`` and the map simply shows nothing.
"""
import json
import os
import threading
import time
import urllib.request

import dbhandler

GEO_TTL = 6 * 3600          # re-geolocate peers at most every 6 hours
GEO_API = "http://ip-api.com/batch"   # free tier: http only, 100 IPs/batch, ~15 req/min — fine when cached


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


def _open_db(node):
    return dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                               node.ledger_ram_file, node.logger, trace_db_calls=node.trace_db_calls)


# ------------------------------------------------------- transactions-per-month (cached) ----
# Non-coinbase, positive-height transactions grouped by calendar month. reward=0 excludes the coinbase;
# negative heights (dev/HN reward mirrors) are excluded by block_height>0.
_TXM_SQL = ("SELECT strftime('%Y-%m', timestamp, 'unixepoch') AS m, COUNT(*) "
            "FROM transactions WHERE block_height > ? AND block_height <= ? AND reward = 0 "
            "GROUP BY m")


def _txm_compute(node):
    if getattr(node, "_stats_txm_computing", False):
        return
    node._stats_txm_computing = True

    def work():
        try:
            d = _open_db(node)
            try:
                tip = int(getattr(node, "hdd_block", 0) or 0)
                rows = d.fetchall(d.h, _TXM_SQL, (0, tip))
                months = {m: int(c) for (m, c) in rows if m}
                cache = {"height": tip, "months": months,
                         "total": sum(months.values())}
                node._stats_txm_cache = cache
                _save(node, "txmonth", cache)
            finally:
                d.close()
        except Exception as e:
            try:
                node.logger.app_log.warning("tx-per-month compute failed: %s" % e)
            except Exception:
                pass
        finally:
            node._stats_txm_computing = False

    threading.Thread(target=work, daemon=True).start()


def tx_per_month(node, db):
    """Cached monthly histogram of non-coinbase transactions. Returns a dict with ``status`` ('ok' |
    'computing'), ``height``, ``total`` and ``months`` (sorted list of {month, count}). The cold scan runs
    in the background; once cached it is topped up incrementally over only the new blocks per call."""
    tip = int(getattr(node, "hdd_block", 0) or 0)
    cache = getattr(node, "_stats_txm_cache", None)
    if cache is None:
        cache = _load(node, "txmonth")
        if cache is not None:
            node._stats_txm_cache = cache
    if cache is not None:
        if tip > int(cache.get("height", 0)):
            try:                                    # cheap incremental top-up over the new blocks only
                rows = db.fetchall(db.h, _TXM_SQL, (int(cache["height"]), tip))
                months = dict(cache.get("months", {}))
                for (m, c) in rows:
                    if m:
                        months[m] = months.get(m, 0) + int(c)
                cache = {"height": tip, "months": months, "total": sum(months.values())}
                node._stats_txm_cache = cache
                _save(node, "txmonth", cache)
            except Exception:
                pass
        ms = sorted(cache.get("months", {}).items())
        return {"status": "ok", "height": cache["height"], "total": cache.get("total", 0),
                "months": [{"month": m, "count": c} for m, c in ms]}
    _txm_compute(node)
    return {"status": "computing", "height": tip, "months": []}


# ------------------------------------------------------------ difficulty series (cheap) ----
def difficulty_series(db, tip, points=180):
    """Sample the per-block difficulty from the indexed misc table at ~`points` evenly spaced heights, and
    attach each sampled height's block timestamp (one ranged query over the transactions index). Cheap —
    no full scan. Returns [{height, difficulty, timestamp}] ascending by height."""
    tip = int(tip or 0)
    if tip <= 0:
        return []
    step = max(1, tip // max(1, points))
    # `%` is SQLite's modulo operator here (qmark params -> no Python %-formatting on this string).
    rows = db.fetchall(db.h, "SELECT block_height, difficulty FROM misc "
                             "WHERE block_height > 0 AND (block_height % ?) = 0 "
                             "ORDER BY block_height ASC", (step,))
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
