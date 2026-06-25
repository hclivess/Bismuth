# optiexplorer.py — Optipoolware web dashboard
# Modernized: stdlib http.server (no Flask, no Tornado), a small JSON API, and a dark live dashboard.
# Copyright Hclivess, Maccaspacca, vv181 2017-2018; modernization 2026. See LICENSE.
#
# Data sources:
#   * shares.db / archive.db  -> per-miner shares, hashrate, workers, round totals (local pool state).
#   * the NODE's REST API     -> network height/difficulty, hf2 fork status, and the pool address's
#                                coinbase rewards + recent payouts. This REPLACES the old per-request
#                                full scan of static/ledger.db (the 23GB live ledger) — never touch it.
#
# Serves:
#   GET /            -> the dashboard HTML (templates/index.html), which fetch()es /api/stats and refreshes.
#   GET /api/stats   -> JSON: { network, miners, round, payouts, pending } for the dashboard / external use.
import json
import os
import sqlite3
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# the node's modernized libs from the repo root when run in-repo
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import essentials  # noqa: E402
import options  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 9080

# --- config -------------------------------------------------------------------------------------
_cfg = options.Get()
_cfg.read()
NODE_IP = getattr(_cfg, "node_ip", "127.0.0.1")
REST_PORT = int(getattr(_cfg, "rest_api_port", 5659))
REST_BASE = "http://%s:%d/api" % (NODE_IP, REST_PORT)

# pool payout address (only the address is needed here)
try:
    _loaded = essentials.keys_load("privkey.der", "pubkey.der")
    POOL_ADDRESS = _loaded[6]
except Exception:
    POOL_ADDRESS = ""


def _pool_txt(key, default, cast=str):
    try:
        for line in open(os.path.join(_HERE, "pool.txt")):
            if line.strip().startswith(key + "="):
                return cast(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return default


M_TIMEOUT = _pool_txt("m_timeout", 5, int)         # minutes; hashrate older than this counts as 0
POOL_FEE = _pool_txt("pool_fee", 1, float)
MIN_PAYOUT = _pool_txt("min_payout", 100000, float)
MINING_IP = _pool_txt("mining_ip", NODE_IP)
MINING_PORT = _pool_txt("port", "8525")


# --- node REST (cached so the dashboard never hammers the node) ----------------------------------
def _rest(path, timeout=6):
    with urllib.request.urlopen(REST_BASE + path, timeout=timeout) as r:
        return json.load(r)


_CACHE = {"t": 0.0, "data": None}
_CACHE_TTL = 20.0   # seconds


def _network_and_ledger():
    """Network status (height/diff/fork) + the pool's coinbase rewards/payouts, from the NODE REST.
    Cached for _CACHE_TTL so repeated dashboard polls don't re-hit the node. Never scans the ledger."""
    now = time.time()
    if _CACHE["data"] is not None and (now - _CACHE["t"]) < _CACHE_TTL:
        return _CACHE["data"]
    out = {"height": None, "difficulty": None, "fork": {}, "last_block_age": None,
           "coinbase": [], "payouts": []}
    try:
        st = _rest("/status")
        out["height"] = st.get("blocks") or st.get("block_height")
    except Exception:
        pass
    try:
        out["difficulty"] = _rest("/difficulty").get("difficulty")
    except Exception:
        pass
    try:
        out["fork"] = _rest("/fork")
    except Exception:
        out["fork"] = {}
    if POOL_ADDRESS:
        try:
            txs = _rest("/address/%s/transactions?limit=200" % POOL_ADDRESS).get("transactions", [])
            for t in txs:
                reward = float(t.get("reward") or 0)
                if reward != 0:   # a block this pool mined (coinbase)
                    out["coinbase"].append({"height": t.get("block_height"), "reward": reward,
                                            "timestamp": t.get("timestamp")})
                if t.get("openfield") == "pool":   # a payout this pool sent
                    out["payouts"].append({
                        "recipient": t.get("recipient"), "amount": t.get("amount"),
                        "height": t.get("block_height"),
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(float(t.get("timestamp") or 0))),
                    })
        except Exception:
            pass
    _CACHE["data"], _CACHE["t"] = out, now
    return out


# --- pool state from shares.db (preserves the original per-miner/hashrate logic) ------------------
def _miners_and_round():
    out = {"miners": [], "shares_total": 0, "pool_hashrate_khs": 0, "workers": 0, "block_threshold": time.time()}
    try:
        sh = sqlite3.connect(os.path.join(_HERE, "shares.db"))
        sh.text_factory = str
        s = sh.cursor()
    except Exception:
        return out
    try:
        addresses = []
        for row in s.execute("SELECT DISTINCT address FROM shares"):
            addresses.append(row[0])

        total_hash = 0
        worker_count = 0
        unpaid_shares = []
        first_ts = []
        now = time.time()

        for x in addresses:
            s.execute("SELECT sum(shares) FROM shares WHERE address = ? AND paid != 1", (x,))
            shares_sum = s.fetchone()[0]
            if shares_sum is None:
                continue
            unpaid_shares.append((x, shares_sum))

            s.execute("SELECT timestamp FROM shares WHERE address = ? ORDER BY timestamp ASC LIMIT 1", (x,))
            first_ts.append(float(s.fetchone()[0]))

            s.execute("SELECT subname FROM shares WHERE address = ? ORDER BY timestamp DESC LIMIT 1", (x,))
            last_worker = s.fetchone()[0]

            # per-worker latest rate/count, zeroed if older than M_TIMEOUT minutes
            mrate = wcount = 0
            last_age = 1e9
            for (name,) in s.execute("SELECT DISTINCT name FROM shares WHERE address = ?", (x,)).fetchall():
                s.execute("SELECT rate, workers, timestamp FROM shares WHERE address = ? AND name = ? "
                          "ORDER BY timestamp DESC LIMIT 1", (x, name))
                rate, workers, ts = s.fetchone()
                age_min = (now - float(ts)) / 60
                last_age = min(last_age, age_min)
                if age_min < M_TIMEOUT:
                    mrate += int(rate)
                    wcount += int(workers)
            total_hash += mrate
            worker_count += wcount

            if last_age < 30:   # active miner
                out["miners"].append({"address": x, "shares": shares_sum, "hashrate_khs": mrate,
                                      "last_worker": last_worker, "workers": wcount})

        shares_total = sum(v for _, v in unpaid_shares)
        out["shares_total"] = shares_total
        out["pool_hashrate_khs"] = total_hash
        out["workers"] = worker_count
        out["block_threshold"] = min(first_ts) if first_ts else now
        out["_unpaid"] = unpaid_shares
    finally:
        sh.close()
    return out


def build_stats():
    pool = _miners_and_round()
    net = _network_and_ledger()

    # this round's reward = pool coinbase rewards since the oldest unpaid share
    bt = pool["block_threshold"]
    round_reward = sum(c["reward"] for c in net["coinbase"]
                       if (c["timestamp"] or 0) and float(c["timestamp"]) >= bt)
    blocks_round = sum(1 for c in net["coinbase"]
                       if (c["timestamp"] or 0) and float(c["timestamp"]) >= bt)
    shares_total = pool["shares_total"] or 0
    rps = (round_reward / shares_total) if shares_total else 0

    pending = []
    if round_reward > 0:
        for addr, sv in pool.get("_unpaid", []):
            pending.append({"address": addr, "reward": "%.8f" % (sv * rps)})

    fork = net.get("fork") or {}
    return {
        "network": {
            "height": net["height"], "difficulty": net["difficulty"],
            "fork_height": fork.get("fork_height"), "fork_active": fork.get("active"),
            "fork_locked_in": fork.get("locked_in"),
            "blocks_to_fork": (int(fork["fork_height"]) - int(net["height"]))
                              if fork.get("fork_height") and net.get("height") else None,
            "pow": "blake2b" if fork.get("active") else "sha224",
        },
        "pool": {
            "address": POOL_ADDRESS, "fee": POOL_FEE, "min_payout": MIN_PAYOUT,
            "mining_ip": MINING_IP, "mining_port": MINING_PORT,
            "hashrate_mhs": round(pool["pool_hashrate_khs"] / 1000.0, 3),
            "miners": len(pool["miners"]), "workers": pool["workers"],
            "shares_round": shares_total, "blocks_round": blocks_round,
            "round_reward": round(round_reward, 8),
            "reward_per_share": "%.8f" % rps,
        },
        "miners": pool["miners"],
        "payouts": net["payouts"][:80],
        "pending": pending,
        "generated": int(time.time()),
    }


# --- stdlib HTTP server (mirrors node rest_api.BismuthRESTServer pattern) -------------------------
def _make_handler():
    index_html = open(os.path.join(_HERE, "templates", "index.html"), "rb").read()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                if not parts:
                    self._send(200, index_html, "text/html; charset=utf-8")
                elif parts == ["api", "stats"]:
                    self._send(200, json.dumps(build_stats()), "application/json")
                else:
                    self._send(404, json.dumps({"error": "not found"}), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}), "application/json")

    return Handler


if __name__ == "__main__":
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), _make_handler())
    httpd.daemon_threads = True
    print("Optipool explorer on http://0.0.0.0:%d  (node REST %s, pool %s)" % (PORT, REST_BASE, POOL_ADDRESS[:16]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
