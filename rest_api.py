"""
Modern, parallel REST/JSON API for Bismuth nodes.

This runs in its own ``ThreadingHTTPServer`` thread *alongside* — and is intended to eventually replace —
the legacy length-prefixed-JSON socket protocol, so existing peers and wallets keep working unchanged
during the transition. Unlike that protocol's serial request/response pipeline over a single socket,
every HTTP request here is served concurrently in its own thread, each with its own short-lived DB handler.

Enable it in config.txt:

    rest_api=True
    rest_api_port=5659      # optional; defaults to 5659 (regnet/testnet pick their own in node.py)
    rest_api_write=True     # optional; enables POST /api/transaction (tx submission over REST)

READS (GET) are always available when ``rest_api`` is on and never affect consensus. WRITES — transaction
submission — are the post-hardfork transport that moves submission OFF the socket protocol onto the API,
so a wallet needs only HTTP. A submitted tx goes through the SAME ``mempool.merge`` validation the socket
``mpinsert`` uses (signature, balance, dup, format) — the REST endpoint is a new transport, NOT a new rule
or a consensus bypass. It is gated by ``rest_api_write`` (off by default) so a read-only node stays
read-only until an operator opts in.

Endpoints:
    GET  /api                                  - this index
    GET  /api/status                           - node status
    GET  /api/difficulty                       - current difficulty
    GET  /api/block/height/{n}                 - block at a height
    GET  /api/block/hash/{hash}                - block by hash
    GET  /api/balance/{address}                - address balance
    GET  /api/transaction/{txid}               - transaction by id (signature prefix)
    GET  /api/address/{address}/transactions   - recent txs for an address (?limit=N, max 500)
    GET  /api/mempool                          - pending transactions
    GET  /api/peers                            - known peers
    POST /api/transaction                      - submit a signed tx (needs rest_api_write); body:
                                                 {"transaction": [ts, address, recipient, amount,
                                                  signature, public_key, operation, openfield]}
"""
import json
import os
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import amounts
import dbhandler
import essentials
import mempool as mp
import transport
from quantizer import quantize_eight

__version__ = "0.1.0"

MEMPOOL_FIELDS = ("timestamp", "address", "recipient", "amount",
                  "signature", "public_key", "operation", "openfield")


class _NotFound(Exception):
    pass


class _BadRequest(Exception):
    pass


class _Forbidden(Exception):
    pass


# --- circulating-supply cache persistence -------------------------------------------------------
# The cold full-ledger SUM(reward)-SUM(fee) scan is multi-minute on a multi-GB mainnet ledger. The
# computed value is memoized on node._supply_cache and topped up incrementally as the tip advances,
# but an in-memory-only cache is wiped on every restart -> the next /api/supply would trigger another
# full scan (the explorer shows "computing" for minutes). We therefore mirror the cache to a small
# JSON file next to the ledger so a restart reloads it and only the cheap incremental top-up runs.

def supply_cache_path(node):
    """Path of the on-disk supply cache, alongside the ledger and NAMESPACED BY LEDGER FILENAME
    (e.g. static/supply_cache-ledger.db.json). Mainnet and regnet share one static/ directory, so a
    shared filename let regnet test runs overwrite the mainnet cache (the intmode check happened to
    reject the cross-load, but only because regnet runs integer amounts — the namespacing makes the
    isolation structural, like fork.lockin_path). The legacy shared supply_cache.json is ignored."""
    ledger_path = getattr(node, "ledger_path", None)
    if not ledger_path:
        return None
    base = os.path.basename(ledger_path) or "ledger.db"
    return os.path.join(os.path.dirname(ledger_path) or ".", "supply_cache-%s.json" % base)


def load_supply_cache(node, intmode):
    """Load the persisted supply cache into node._supply_cache when present, well-formed, and matching
    the node's current storage mode. Returns the loaded dict (also set on the node) or None. Any
    missing/corrupt/empty/mismatched file is ignored so the caller falls back to a fresh compute."""
    path = supply_cache_path(node)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            raw = json.load(f)
        if bool(raw.get("intmode")) != bool(intmode):
            return None  # storage-mode cutover: the persisted figure is in the other unit basis
        cache = {"height": int(raw["height"]),
                 "circ": Decimal(str(raw["circ"])),  # string -> Decimal, no float drift
                 "intmode": bool(raw["intmode"])}
    except Exception as e:
        try:
            node.logger.app_log.warning("supply cache load failed ({}): {}".format(path, e))
        except Exception:
            pass
        return None
    node._supply_cache = cache
    return cache


def save_supply_cache(node, cache):
    """Persist the supply cache to disk (circ as a decimal string). Best-effort; failures are logged
    but never raised, so reporting never disturbs node operation. Written atomically via a temp file."""
    path = supply_cache_path(node)
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"height": int(cache["height"]),
                       "circ": str(cache["circ"]),
                       "intmode": bool(cache["intmode"])}, f)
        os.replace(tmp, path)
    except Exception as e:
        try:
            node.logger.app_log.warning("supply cache save failed ({}): {}".format(path, e))
        except Exception:
            pass


def start_supply_compute(node, height=None, intmode=None):
    """Run the cold full-ledger supply scan in a daemon thread (so it never blocks a request / 504s),
    store the result on node._supply_cache, and persist it to disk so future restarts skip the scan.
    Idempotent: a no-op while a compute is already in flight. Called on demand by /api/supply and once
    proactively at REST-server start so the first-ever compute (no cache file yet) runs ahead of demand."""
    if getattr(node, "_supply_computing", False):
        return
    if height is None:
        height = int(getattr(node, "hdd_block", 0) or 0)
    if intmode is None:
        intmode = bool(getattr(node, "ledger_integer_amounts", False))
    node._supply_computing = True

    def work():
        try:
            d = dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                                    node.ledger_ram_file, node.logger,
                                    trace_db_calls=node.trace_db_calls)
            try:
                base = d.fetchall(d.h, "SELECT COALESCE(SUM(reward),0)-COALESCE(SUM(fee),0) "
                                       "FROM transactions WHERE block_height >= 0")[0][0]
                mirror = d.fetchall(d.h, "SELECT COALESCE(SUM(amount),0) FROM transactions "
                                         "WHERE block_height < 0")[0][0]
                if intmode:
                    circ = amounts.to_decimal(int(base or 0)) + amounts.to_decimal(int(mirror or 0))
                else:
                    circ = quantize_eight(base or 0) + quantize_eight(mirror or 0)
                node._supply_cache = {"height": height, "circ": circ, "intmode": intmode}
                save_supply_cache(node, node._supply_cache)  # persist so a restart skips the scan
            finally:
                d.close()
        except Exception as e:
            node.logger.app_log.warning("supply compute failed: {}".format(e))
        finally:
            node._supply_computing = False

    threading.Thread(target=work, daemon=True).start()


def _make_handler(node):
    """Build a request-handler class bound to the running ``node`` instance."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # silence the default stderr access log
            return

        def _write(self, code, payload):
            body = json.dumps(payload).encode("utf-8")
            # Transport compression at the HTTP layer (standard Content-Encoding) — this is where
            # bandwidth savings for parallel block fetching live, not in the legacy socket protocol.
            http_enc, codec = self._negotiate_encoding()
            if codec:
                body = transport.compress(codec, body)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            if http_enc:
                self.send_header("Content-Encoding", http_enc)
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _negotiate_encoding(self):
            """Pick an HTTP Content-Encoding. An explicit ?compress= override wins (none/identity =>
            plaintext, the documented way to read the raw API; gzip|br => force that codec); otherwise
            fall back to the request's Accept-Encoding. Returns (http_name, transport_codec) or
            ("", None) for an uncompressed response."""
            override = getattr(self, "_compress_override", None)
            if override is not None:
                if override in ("", "none", "identity", "plain", "raw"):
                    return "", None
                forced = {"gzip": ("gzip", "gzip"), "br": ("br", "brotli"),
                          "brotli": ("br", "brotli")}.get(override)
                if forced and transport.is_supported(forced[1]):
                    return forced
                return "", None  # unknown/unsupported override -> safe plaintext
            accepted = {e.strip().split(";")[0]
                        for e in (self.headers.get("Accept-Encoding") or "").split(",")}
            for http_name, codec in (("br", "brotli"), ("gzip", "gzip")):  # preference order
                if http_name in accepted and transport.is_supported(codec):
                    return http_name, codec
            return "", None

        def do_GET(self):
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            query = parse_qs(parsed.query)
            # Explicit transport override (nado-style): ?compress=none forces plaintext (the documented
            # way to read the raw API), ?compress=gzip|br forces that codec; absent => gzip/br is the
            # default for any client that advertises Accept-Encoding (browsers, our rest_client).
            self._compress_override = (query.get("compress") or [None])[0]
            db = None
            try:
                # parts[0] must be "api"
                # Self-describing welcome page, served at both "/" and "/api".
                if not parts:
                    return self._write(200, self._index())
                if parts[0] != "api":
                    raise _NotFound("use /api")
                route = parts[1:]
                if not route:
                    return self._write(200, self._index())
                if route == ["status"]:
                    return self._write(200, self._status())
                if route == ["difficulty"]:
                    return self._write(200, self._difficulty())
                if route == ["peers"]:
                    return self._write(200, self._peers())
                if route == ["nodes"]:
                    return self._write(200, self._nodes())
                if route == ["vm", "contracts"]:
                    return self._write(200, self._vm_contracts())
                if route[:2] == ["vm", "contract"] and len(route) == 3:
                    return self._write(200, self._vm_contract(route[2]))
                if route[:2] == ["vm", "market"] and len(route) == 3:
                    return self._write(200, self._vm_market(route[2]))
                if route == ["shield", "stats"]:
                    return self._write(200, self._shield_stats())
                if route[:2] == ["shield", "note"] and len(route) == 3:
                    return self._write(200, self._shield_note(route[2]))
                if route == ["fee"]:
                    return self._write(200, self._fee())
                if route == ["capabilities"]:
                    return self._write(200, self._capabilities())

                db = self._db()
                if route == ["mempool"]:
                    return self._write(200, self._mempool())
                if route[:2] == ["block", "height"] and len(route) == 3:
                    return self._write(200, self._block_by_height(db, route[2]))
                if route[:2] == ["block", "hash"] and len(route) == 3:
                    return self._write(200, self._block_by_hash(db, route[2]))
                if route[:2] == ["blocks", "since"] and len(route) == 3:
                    return self._write(200, self._blocks_since(db, route[2], query))
                if route[:2] == ["blocks", "range"] and len(route) == 4:
                    return self._write(200, self._blocks_range(db, route[2], route[3], query))
                if route[:2] == ["headers", "range"] and len(route) == 4:
                    return self._write(200, self._headers_range(db, route[2], route[3]))
                if route[:1] == ["balance"] and len(route) == 2:
                    return self._write(200, self._balance(db, route[1]))
                if route[:1] == ["transaction"] and len(route) >= 2:
                    # a txid is a base64 signature prefix and may contain "/", so rejoin the tail
                    return self._write(200, self._transaction(db, "/".join(route[1:])))
                if route[:1] == ["address"] and len(route) == 3 and route[2] == "transactions":
                    return self._write(200, self._address_txs(db, route[1], query))
                if route == ["fork"]:
                    return self._write(200, self._fork(db))
                if route == ["supply"]:
                    return self._write(200, self._supply(db))
                # Modern plugins (doc/27) may own a route — the tokens_aliases plugin serves /api/tokens and
                # /api/token/<name> from its own LMDB store, so post-fork no token/alias code is left in this
                # router. When no plugin claims the route, fall through to the core (legacy index.db) handlers.
                handled, result = node.plugin_manager.rest_handle("GET", route, query)
                if handled:
                    if result is None:
                        raise _NotFound("unknown token")
                    return self._write(200, result)
                if route == ["tokens"]:
                    return self._write(200, self._tokens(db))
                if route[:1] == ["token"] and len(route) == 2:
                    return self._write(200, self._token(db, route[1], query))
                raise _NotFound("unknown endpoint")
            except _NotFound as e:
                self._write(404, {"error": "not_found", "detail": str(e)})
            except _BadRequest as e:
                self._write(400, {"error": "bad_request", "detail": str(e)})
            except Exception as e:
                self._write(500, {"error": "server_error", "detail": str(e)})
            finally:
                if db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass

        def do_POST(self):
            """WRITE path. Post-hardfork, transaction submission moves OFF the legacy socket protocol onto
            the REST API; this is that endpoint. Disabled unless ``rest_api_write`` is set (the GET API
            stays consensus-neutral by default). A submitted tx goes through the SAME mempool.merge
            validation the socket ``mpinsert`` uses — the API is just the new transport, no new rules."""
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            self._compress_override = (parse_qs(parsed.query).get("compress") or [None])[0]
            db = None
            try:
                if parts[:1] != ["api"]:
                    raise _NotFound("use /api")
                route = parts[1:]
                if route in (["transaction"], ["sendtx"], ["mempool"]):
                    if not getattr(node, "rest_api_write", False):
                        raise _Forbidden("transaction submission is disabled; start the node with rest_api_write=True")
                    payload = self._read_body_json()
                    db = self._db()
                    return self._write(200, self._submit_transaction(db, payload))
                raise _NotFound("unknown endpoint")
            except _NotFound as e:
                self._write(404, {"error": "not_found", "detail": str(e)})
            except _Forbidden as e:
                self._write(403, {"error": "forbidden", "detail": str(e)})
            except _BadRequest as e:
                self._write(400, {"error": "bad_request", "detail": str(e)})
            except Exception as e:
                self._write(500, {"error": "server_error", "detail": str(e)})
            finally:
                if db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass

        def _read_body_json(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise _BadRequest("bad Content-Length")
            if n <= 0 or n > 2_000_000:               # generous (a RingCT spend openfield is ~95 KB)
                raise _BadRequest("missing or oversized request body")
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                raise _BadRequest("bad json: %s" % e)

        def _submit_transaction(self, db, payload):
            """Insert a signed transaction (or several) into the mempool, like the socket ``mpinsert``.
            Accepts an 8-field array, ``{"transaction": [...]}``, or an array of 8-field arrays — exactly
            the wire-form tuple a wallet builds and signs. Validation (signature, balance, dup, format) is
            mempool.merge's job, identical to the socket path; this never bypasses any check."""
            if isinstance(payload, dict):
                payload = payload.get("transaction", payload.get("transactions"))
            if payload is None:
                raise _BadRequest("provide a 'transaction' (8-field array) or an array of them")
            txs = payload if (isinstance(payload, list) and payload and isinstance(payload[0], list)) else [payload]
            for tx in txs:
                if not (isinstance(tx, list) and len(tx) == 8):
                    raise _BadRequest("each transaction must be an 8-field array %s" % (MEMPOOL_FIELDS,))
                if not all(isinstance(f, (str, int, float)) for f in tx):
                    raise _BadRequest("transaction fields must be scalar (wire form)")
            if mp.MEMPOOL is None:
                raise Exception("mempool not ready")
            peer_ip = self.client_address[0] if self.client_address else "127.0.0.1"
            # stringify to the canonical wire form merge expects, then merge (size_bypass + wait, like mpinsert)
            txs = [[str(f) for f in tx] for tx in txs]
            result = mp.MEMPOOL.merge(txs, peer_ip, db.c, True, True)
            return {"submitted": len(txs), "txids": [tx[4][:56] for tx in txs], "result": result}

        # --- helpers -------------------------------------------------------
        def _db(self):
            return dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                                       node.ledger_ram_file, node.logger,
                                       trace_db_calls=node.trace_db_calls)

        def _index(self):
            # Self-describing welcome page (served at "/" and "/api"): the list of API methods and what
            # each does, so the API is discoverable without external docs.
            return {"name": "Bismuth REST API", "version": __version__,
                    "node_version": node.app_version, "protocol": node.version,
                    "transport": "responses are gzip/br compressed when the client sends Accept-Encoding; "
                                 "add ?compress=none for plaintext, ?compress=gzip|br to force a codec",
                    "endpoints": {
                        "/api/status": "node status (height, peers, difficulty, consensus)",
                        "/api/capabilities": "REST/transport capabilities for peer sync (reachable = capable)",
                        "/api/difficulty": "current difficulty detail",
                        "/api/block/height/{n}": "block at a given height",
                        "/api/block/hash/{hash}": "block by hash",
                        "/api/blocks/since/{height}": "positive-height blocks after {height} (?limit=N, parallel sync)",
                        "/api/blocks/range/{start}/{end}": "blocks in the inclusive height range (parallel sync; "
                                                          "?format=sync for consensus-faithful digester tuples)",
                        "/api/headers/range/{start}/{end}": "compact block headers (height/hash/timestamp/txs) for "
                                                            "Bitcoin-style headers-first quick sync",
                        "/api/balance/{address}": "confirmed balance of an address",
                        "/api/transaction/{txid}": "transaction by id (signature prefix)",
                        "/api/address/{address}/transactions": "recent txs for an address (?limit=N, max 500)",
                        "/api/mempool": "pending (unconfirmed) transactions",
                        "/api/peers": "known peers",
                        "/api/nodes": "browse the peer network: per-node height, version, reputation, "
                                      "connected/banned/whitelisted status",
                        "/api/supply": "circulating supply + chain height",
                        "/api/tokens": "all tokens on chain, ranked by transfer volume",
                        "/api/token/{name}": "a token's supply, holder count, and per-address balances",
                        "/api/fork": "hf2 auto-fork readiness: signalling run, lock-in, activation height",
                        "/api/shield/stats": "shielded pool (doc/22): note/nullifier counts, pool value, sink",
                        "/api/shield/note/{note_id}": "public fields of a shielded note (nothing decryptable)",
                        "POST /api/transaction": "submit a signed tx (needs rest_api_write=True); body "
                                                 "{\"transaction\": [ts, address, recipient, amount, "
                                                 "signature, public_key, operation, openfield]} — the "
                                                 "post-hardfork submission path, no socket needed",
                    }}

        def _fork(self, db):
            import fork
            tip = node.hdd_block
            # /api/fork was reliably tens of seconds because fork.db_fork_signal_reader runs ONE point query
            # per block and fork_status scans the whole signalling window (FORK2_WINDOW=1000) — an N+1 pattern
            # (~1000 queries). That compute also exceeds the ~60s mainnet block interval, so a tip-keyed cache
            # alone never helped (the tip moved between calls). Root-cause fix: prefetch the coinbase openfield
            # of EVERY block in the window in a SINGLE ranged query (the max-rowid row per height == the
            # coinbase, exactly matching the reader's per-block `ORDER BY rowid DESC LIMIT 1`), then feed
            # fork_status a dict-backed reader. fork_status's logic is UNCHANGED — only the data access is
            # batched (~0.01s vs ~88s). The tip cache is kept as cheap burst insurance (now actually effective
            # since the compute is sub-second). Cache lives on the closure-captured `node` (process-lifetime).
            cached = getattr(node, "_rest_fork_cache", None)
            if cached is not None and cached[0] == tip:
                return cached[1]
            window = int(getattr(node, "fork_window", fork.FORK2_WINDOW))
            # fork_status -> dynamic_fork_height scans the coinbase signal of EVERY block from genesis to tip
            # (the lock-in is the EARLIEST full window, deliberately tip-independent), so feeding it only the
            # trailing window is wrong — it would never find / would chase the lock-in. Instead fetch the full
            # SET of heights whose coinbase carried the signal in ONE query (the max-rowid row per height ==
            # the coinbase, matching db_fork_signal_reader's `ORDER BY rowid DESC LIMIT 1`; LIKE '%hf2%' ==
            # has_fork_signal's substring test). Then fork_status's scan is an in-memory set loop (~1s) with an
            # IDENTICAL result, vs ~1000s of point queries (the old tens-of-seconds cost). 'hf2' contains a
            # non-hex char so the nonce/state-root hex in the openfield can never false-match. Cached per tip.
            db.execute_param(db.c,
                "SELECT t.block_height FROM transactions t "
                "JOIN (SELECT block_height, MAX(rowid) mr FROM transactions "
                "      WHERE block_height >= 1 AND block_height <= ? GROUP BY block_height) m "
                "ON t.rowid = m.mr WHERE t.openfield LIKE ?", (int(tip), "%" + fork.FORK2_SIGNAL + "%"))
            sigset = set(int(r[0]) for r in db.c.fetchall())
            result = fork.fork_status(lambda h: int(h) in sigset, tip, window,
                                      getattr(node, "fork_boundary", fork.FORK2_BOUNDARY),
                                      getattr(node, "fork_bury", fork.FORK2_BURY))
            node._rest_fork_cache = (tip, result)
            return result

        def _shield_stats(self):
            # shielded pool health (doc/22). pool_units MUST equal the SHIELD_SINK ledger balance — that
            # invariant (publicly checkable, since amounts are transparent) is the supply-safety guarantee.
            st = getattr(node, "shielded_state", None)
            base = {"enabled": st is not None, "fork_height": getattr(node, "fork_height", None)}
            if st is None:
                return base
            import shieldedv1
            base["sink"] = shieldedv1.SHIELD_SINK
            base.update(st.stats())
            return base

        def _shield_note(self, note_id):
            st = getattr(node, "shielded_state", None)
            if st is None:
                raise _NotFound("shielded value not enabled")
            n = st.note(note_id)
            if n is None:
                raise _NotFound("unknown note")
            # public fields only — nothing returned is decryptable without the recipient's view key. With
            # ring spends, whether a note is spent is UNKNOWABLE on-chain (the anonymity); wallets track
            # their own spends via key images. The note serves as a ring decoy + scanning target.
            return {"note_id": n["note_id"], "create_height": n["create_height"], "token": n["token"],
                    "amount": n["amount"], "R": n["r_pub"], "P": n["p_pub"], "commitment": n["commitment"]}

        def _status(self):
            diff = node.difficulty[0] if getattr(node, "difficulty", None) else None
            return {"protocol": node.version, "node_version": node.app_version,
                    "testnet": node.is_testnet, "regnet": node.is_regnet,
                    "address": node.keys.address if node.reveal_address else "",
                    "blocks": node.hdd_block, "last_block_hash": node.last_block_hash,
                    "difficulty": diff, "connections": len(node.peers.connection_pool),
                    "consensus": node.peers.consensus, "threads": threading.active_count(),
                    "uptime": int(time.time() - node.startup_time),
                    "server_timestamp": "%.2f" % time.time()}

        def _capabilities(self):
            # Capability descriptor a peer fetches to decide whether to sync from us over the REST API
            # and which transport codec to negotiate. Reachability of THIS endpoint is itself the test:
            # if a peer can GET it, we are REST-capable — no socket handshake needed (doc/06, doc/15).
            return {"version": node.version, "node_version": node.app_version,
                    "rest_api": True, "rest_port": node.rest_api_port,
                    "compress": transport.available_codecs(),
                    "blocks": node.hdd_block,
                    # post-hardfork transaction submission over REST (POST /api/transaction); when this is
                    # True a client never needs the legacy socket protocol to send a tx.
                    "rest_api_write": bool(getattr(node, "rest_api_write", False)),
                    "testnet": node.is_testnet, "regnet": node.is_regnet}

        def _difficulty(self):
            d = getattr(node, "difficulty", None)
            if not d:
                return {"difficulty": None}
            keys = ["difficulty", "diff_dropped", "time_to_generate", "diff_block_previous",
                    "block_time", "hashrate", "diff_adjustment", "block_height"]
            return dict(zip(keys, d))

        def _peers(self):
            peers = node.peers.peer_dict if node.peers else {}
            return {"count": len(peers), "peers": peers}

        def _nodes(self):
            """Browse the peer network the node sees: each peer's reported height, version, reputation
            (peers_reputation), and connected / banned / whitelisted status — an at-a-glance, well-behaved
            -peers-first network view."""
            p = node.peers
            if not p:
                return {"count": 0, "tip": 0, "consensus": None, "nodes": []}
            opinions = dict(getattr(p, "peer_opinion_dict", {}) or {})
            versions = dict(getattr(p, "ip_to_mainnet", {}) or {})
            connected = set(getattr(p, "_connection_pool_set", set()) or [])
            tip = int(getattr(node, "hdd_block", 0) or 0)
            out = []
            for ip in (set(opinions) | set(versions) | connected):
                h = opinions.get(ip)
                out.append({
                    "ip": ip,
                    "height": h,
                    "behind": (tip - h) if isinstance(h, int) else None,
                    "version": versions.get(ip),
                    "reputation": p.reputation(ip) if hasattr(p, "reputation") else 0,
                    "connected": ip in connected,
                    "banned": p.is_banned(ip) if hasattr(p, "is_banned") else False,
                    "whitelisted": p.is_whitelisted(ip) if hasattr(p, "is_whitelisted") else False,
                })
            out.sort(key=lambda n: (n["connected"], n["reputation"], n["height"] or 0), reverse=True)
            return {"count": len(out), "tip": tip,
                    "consensus": getattr(p, "consensus", None),
                    "consensus_percentage": getattr(p, "consensus_percentage", None),
                    "nodes": out}

        def _fee(self):
            """Current fee parameters. Post-fork the base fee is CONGESTION-responsive (fee_dynamics):
            it scales with recent block WEIGHT (tx count + openfield bytes // w_unit) over a window,
            clamped — wallets should read `base_fee` here for the live minimum. vm: txs additionally pay
            vm_surcharge; +len(openfield)/100000 per tx; shield: txs +0.01."""
            import fee_dynamics
            bf = getattr(node, "base_fee", None)
            return {"base_fee": str(bf) if bf is not None else str(essentials.BASE_FEE),
                    "static_base_fee": str(essentials.BASE_FEE),
                    "post_fork": bool(getattr(node, "fee_post_fork", False)),
                    "vm_surcharge": str(fee_dynamics.VM_SURCHARGE),
                    "congestion": "weight", "target_weight": fee_dynamics.TARGET_WEIGHT,
                    "weight_unit_bytes": fee_dynamics.W_UNIT, "window": fee_dynamics.WINDOW,
                    "min_mult": str(fee_dynamics.MIN_MULT), "max_mult": str(fee_dynamics.MAX_MULT),
                    "target_txs": fee_dynamics.TARGET_TXS}

        def _vm_contracts(self):
            """Deployed decentralized-apps VM contracts. Empty unless vm=True and the fork has activated."""
            vms = getattr(node, "vm_state", None)
            if vms is None:
                return {"enabled": False, "fork_height": getattr(node, "fork_height", None), "contracts": []}
            addrs = vms.list_contracts()
            return {"enabled": True, "fork_height": getattr(node, "fork_height", None),
                    "state_root": getattr(node, "vm_state_root", None),
                    "count": len(addrs), "contracts": addrs}

        def _vm_contract(self, addr):
            """A contract's bytecode + storage (decentralized-apps v2)."""
            vms = getattr(node, "vm_state", None)
            if vms is None:
                raise _NotFound("vm not enabled")
            code = vms.get_code(addr)
            if code is None:
                raise _NotFound("unknown contract")
            import vm_engine
            body = code or b""                  # raw RV32I code (the single engine; no tag)
            storage = [{"key": str(k), "value": str(v)} for k, v in vms.storage_items(addr)]
            return {"address": addr, "engine": vm_engine.ENGINE_NAME, "code": body.hex(), "code_size": len(body),
                    "balance": str(vms.get_balance(addr)),   # BIS custody held by the contract (units)
                    "slots": len(storage), "storage": storage}

        def _vm_market(self, addr):
            """Read-only DECODER for the binary prediction-market contract (contracts/prediction_market.py)
            so a UI doesn't need to know the slot layout. Consensus-neutral: it only re-reads the same
            committed VM state /api/vm/contract exposes, mapping the fixed slots to named fields:
              slot 1 = resolved (0/1), 2 = outcome (1=YES,2=NO), 3 = yes_pot units, 4 = no_pot units.
            'pot' is the contract's live custody balance. Implied odds are pool-share percentages. Returns
            404 only if the VM is off or the contract is unknown; any contract is decodable (the caller is
            responsible for pointing at an actual market — fields are simply 0 for a non-market contract)."""
            vms = getattr(node, "vm_state", None)
            if vms is None:
                raise _NotFound("vm not enabled")
            if vms.get_code(addr) is None:
                raise _NotFound("unknown contract")
            slots = dict(vms.load_storage(addr))          # {int_key: int_val}
            S_RESOLVED, S_OUTCOME, S_YESPOT, S_NOPOT = 1, 2, 3, 4
            yes_pot = int(slots.get(S_YESPOT, 0))
            no_pot = int(slots.get(S_NOPOT, 0))
            resolved = int(slots.get(S_RESOLVED, 0))
            outcome = int(slots.get(S_OUTCOME, 0))
            total = yes_pot + no_pot
            yes_odds = round(100.0 * yes_pot / total, 2) if total else 0.0
            no_odds = round(100.0 * no_pot / total, 2) if total else 0.0
            return {"address": addr,
                    "yes_pot": str(yes_pot), "no_pot": str(no_pot),
                    "pot": str(vms.get_balance(addr)),         # live custody (units) = claimable pool
                    "resolved": bool(resolved),
                    "outcome": outcome,                        # 0 unresolved, 1 YES, 2 NO
                    "outcome_label": {1: "YES", 2: "NO"}.get(outcome, ""),
                    "yes_odds": yes_odds, "no_odds": no_odds}  # implied probability, % of the pool

        def _supply(self, db):
            """Circulating supply = mining emission (sum(reward)-sum(fee) over positive heights) + the
            concluded dev/HN reward mirrors (negative heights), from the full ledger (db.h = hdd). Cached
            on the node and kept up to date INCREMENTALLY as the tip advances. The cold full-ledger scan
            (potentially many GB) runs in a BACKGROUND thread so it never blocks the request / 504s; the
            first call returns status "computing", later calls return the value."""
            height = int(getattr(node, "hdd_block", 0) or 0)
            intmode = bool(getattr(node, "ledger_integer_amounts", False))
            cache = getattr(node, "_supply_cache", None)
            if cache is None:  # cold start (e.g. just restarted): try the on-disk cache before scanning
                cache = load_supply_cache(node, intmode)
            if cache is not None and cache.get("intmode") == intmode:
                if height <= cache["height"]:
                    return {"height": cache["height"], "status": "ok",
                            "circulating": str(quantize_eight(cache["circ"]))}
                try:  # tip advanced -> cheap incremental top-up over just the new blocks
                    r = db.fetchall(db.h, "SELECT COALESCE(SUM(reward),0)-COALESCE(SUM(fee),0) FROM "
                                          "transactions WHERE block_height > ? AND block_height <= ?",
                                    (cache["height"], height))[0][0]
                    add = amounts.to_decimal(int(r or 0)) if intmode else quantize_eight(r or 0)
                    node._supply_cache = {"height": height, "circ": cache["circ"] + add, "intmode": intmode}
                    save_supply_cache(node, node._supply_cache)  # keep the on-disk cache at the new tip
                    return {"height": height, "status": "ok",
                            "circulating": str(quantize_eight(node._supply_cache["circ"]))}
                except Exception:
                    pass
            start_supply_compute(node, height, intmode)
            return {"height": height, "circulating": None, "status": "computing"}

        def _tokens(self, db):
            """All tokens seen on chain, ranked by transfer volume."""
            # SEAM (doc/26 stage 2): read the isolated LMDB side-index when enabled, else index.db SQL.
            if node.token_index is not None:
                rows = node.token_index.tokens_list(500)
                return {"count": len(rows), "tokens": [{"token": t, "transfers": c} for t, c in rows]}
            rows = db.fetchall(db.index_cursor, "SELECT token, COUNT(*) FROM tokens "
                                                "GROUP BY token ORDER BY COUNT(*) DESC LIMIT 500")
            return {"count": len(rows), "tokens": [{"token": r[0], "transfers": r[1]} for r in rows]}

        def _token(self, db, name, query):
            """A token's holders, per-address balances, and supply (credits - debits, token index).

            The `holders` array is windowed by ?limit (default 200, max 1000) and ?offset (default 0), with
            explicit `holder_count` (total), `holders_returned`, `offset`, `limit` and `truncated` fields so a
            consumer can tell when the list is partial and page through the rest. (It used to silently cap
            holders at the top 200 by balance with no flag, so a naive reader summing the array got a value
            below the canonical `supply` and could not tell it was incomplete.) `supply`/`holder_count` are
            always the full-set aggregates regardless of the window. NOTE: when the tokens_aliases plugin is
            loaded it owns the /api/token route (see do_GET) and this handler is the legacy index.db fallback;
            both paths share the same windowing semantics (the plugin's token_detail applies them internally)."""
            try:
                limit = int(query.get("limit", ["200"])[0])
                offset = int(query.get("offset", ["0"])[0])
            except ValueError:
                raise _BadRequest("limit/offset must be integers")
            limit = max(1, min(limit, 1000))
            offset = max(0, offset)
            if node.token_index is not None:
                detail = node.token_index.token_detail(name, limit, offset)   # windows + flags internally
                if detail is None:
                    raise _NotFound("unknown token")
                return detail
            credits = {r[0]: int(r[1] or 0) for r in db.fetchall(db.index_cursor,
                       "SELECT recipient, SUM(amount) FROM tokens WHERE token = ? GROUP BY recipient", (name,))}
            debits = {r[0]: int(r[1] or 0) for r in db.fetchall(db.index_cursor,
                      "SELECT address, SUM(amount) FROM tokens WHERE token = ? GROUP BY address", (name,))}
            holders = []
            for addr in (set(credits) | set(debits)):
                bal = credits.get(addr, 0) - debits.get(addr, 0)
                if bal > 0:
                    holders.append({"address": addr, "balance": bal})
            holders.sort(key=lambda h: -h["balance"])
            transfers = db.fetchall(db.index_cursor, "SELECT COUNT(*) FROM tokens WHERE token = ?", (name,))[0][0]
            if not holders and not transfers:
                raise _NotFound("unknown token")
            total = len(holders)
            window = holders[offset:offset + limit]
            return {"token": name, "supply": sum(h["balance"] for h in holders),
                    "holder_count": total, "transfers": transfers, "holders": window,
                    "holders_returned": len(window), "offset": offset, "limit": limit,
                    "truncated": (offset + len(window)) < total}

        def _block_rows_to_json(self, rows):
            return [essentials.format_raw_tx(row) for row in rows]

        # --- storage read seam (doc/26 stage 3) -----------------------------------------------------
        # The block-BODY reads below go through the seam: post-fork, when the LMDB block store is present,
        # they read from it (canonical post-fork store); otherwise the legacy ledger.db cursor (pre-fork =
        # unchanged). Both return the same 12-field rows (proven by storage_backend.cross_check), so the JSON
        # is identical. LMDB-first-with-SQLite-FALLBACK keeps it correct on a node whose store was enabled
        # mid-chain (older blocks not yet in LMDB fall back to SQLite). Display-only; never consensus.
        def _store_backend(self):
            store = getattr(node, "block_store", None)
            fork_height = getattr(node, "fork_height", None)
            last_block = getattr(node, "last_block", 0) or 0
            if store is not None and fork_height is not None and last_block >= fork_height:
                import storage_backend
                return storage_backend.LmdbBackend(store)
            return None

        def _block_rows(self, db, height):
            be = self._store_backend()
            if be is not None:
                rows = be.get_block(height)
                if rows:                                   # in LMDB; else fall back (pre-enable history)
                    return rows
            return db.fetchall(db.h, "SELECT * FROM transactions WHERE block_height = ?", (height,))

        def _range_rows(self, db, start, end):
            be = self._store_backend()
            if be is not None:
                tip = be.tip_height()
                # the LMDB store is contiguous to its tip, so start present + tip>=end => whole range present
                if tip is not None and tip >= end and be.get_block(start) is not None:
                    return [r for _h, rows in be.blocks_in_range(start, end) for r in rows]
            return db.fetchall(db.h, "SELECT * FROM transactions WHERE block_height >= ? AND block_height <= ? "
                                     "ORDER BY block_height ASC", (start, end))

        def _block_by_height(self, db, raw_height):
            try:
                height = int(raw_height)
            except ValueError:
                raise _BadRequest("height must be an integer")
            rows = self._block_rows(db, height)
            if not rows:
                raise _NotFound("no block at height {}".format(height))
            return {"block_height": height, "transactions": self._block_rows_to_json(rows)}

        def _block_by_hash(self, db, block_hash):
            be = self._store_backend()
            if be is not None:
                h = be.height_by_hash(block_hash)
                if h is not None:
                    rows = be.get_block(h)
                    if rows:
                        return {"block_hash": block_hash, "block_height": rows[0][0],
                                "transactions": self._block_rows_to_json(rows)}
            rows = db.fetchall(db.h, "SELECT * FROM transactions WHERE block_hash = ?", (block_hash,))
            if not rows:
                raise _NotFound("no block with hash {}".format(block_hash))
            return {"block_hash": block_hash, "block_height": rows[0][0],
                    "transactions": self._block_rows_to_json(rows)}

        def _grouped_blocks(self, rows):
            blocks = {}
            for row in rows:
                blocks.setdefault(row[0], []).append(essentials.format_raw_tx(row))
            return [{"block_height": h, "transactions": txs} for h, txs in blocks.items()]

        def _row_to_sync_tx(self, row):
            # Consensus-faithful tx tuple in the EXACT order the digester consumes
            # (timestamp, address, recipient, amount, signature, public_key, operation, openfield).
            # Unlike format_raw_tx this keeps the public key BASE64-ENCODED as stored — decoding it (as
            # the display API does) would corrupt the bytes the signature is verified against. amount is
            # reconstructed to its decimal value so the digester re-derives the exact signed string.
            return [row[1], row[2], row[3], amounts.display_amount(row[4]),
                    row[5], row[6], row[10], row[11]]

        def _grouped_sync_blocks(self, rows):
            # Group rows into blocks (txs kept in stored/consensus order), carrying the block_hash so a
            # headers-first client can verify each downloaded body against the header it already trusts.
            blocks, order = {}, []
            for row in rows:
                h = row[0]
                if h not in blocks:
                    blocks[h] = {"block_height": h, "block_hash": row[7], "transactions": []}
                    order.append(h)
                blocks[h]["transactions"].append(self._row_to_sync_tx(row))
            return [blocks[h] for h in order]

        def _headers_range(self, db, raw_start, raw_end):
            # Compact per-block headers (height, hash, timestamp, tx count) for Bitcoin-style
            # headers-first sync: a client pulls the cheap header chain, validates linkage / picks the
            # best chain, THEN fetches full bodies in parallel and checks each body re-hashes to its
            # header. block_hash chains to the previous block (consensus), so a contiguous, hash-verified
            # body sequence IS the validated chain — the header pass makes catch-up bandwidth-cheap first.
            try:
                start, end = int(raw_start), int(raw_end)
            except ValueError:
                raise _BadRequest("start and end must be integers")
            if end < start:
                raise _BadRequest("end must be >= start")
            end = min(end, start + 5000)  # headers are tiny; allow a much wider span than full blocks
            rows = db.fetchall(db.h,
                               "SELECT block_height, MAX(block_hash), MIN(timestamp), COUNT(*) "
                               "FROM transactions WHERE block_height >= ? AND block_height <= ? "
                               "GROUP BY block_height ORDER BY block_height ASC", (start, end))
            headers = [{"block_height": r[0], "block_hash": r[1], "timestamp": r[2], "txs": r[3]}
                       for r in (rows or [])]
            return {"start": start, "end": end, "count": len(headers), "headers": headers}

        def _blocks_since(self, db, raw_since, query):
            # Positive-height blocks after `since` (mirror reward rows are regenerated by the digester,
            # so they are not shipped). Designed for parallel range fetching by new sync clients.
            try:
                since = int(raw_since)
            except ValueError:
                raise _BadRequest("height must be an integer")
            try:
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                raise _BadRequest("limit must be an integer")
            limit = max(1, min(limit, 1000))
            # seam: > since AND <= since+limit  ==  the inclusive range [since+1, since+limit]
            rows = self._range_rows(db, since + 1, since + limit)
            return {"since": since, "limit": limit, "blocks": self._grouped_blocks(rows or [])}

        def _blocks_range(self, db, raw_start, raw_end, query=None):
            try:
                start, end = int(raw_start), int(raw_end)
            except ValueError:
                raise _BadRequest("start and end must be integers")
            if end < start:
                raise _BadRequest("end must be >= start")
            end = min(end, start + 1000)  # cap the span
            rows = self._range_rows(db, start, end)
            fmt = (query or {}).get("format", ["json"])[0]
            if fmt == "sync":
                # consensus-faithful tuples a syncing peer can feed straight to its digester
                return {"start": start, "end": end, "format": "sync",
                        "blocks": self._grouped_sync_blocks(rows or [])}
            return {"start": start, "end": end, "blocks": self._grouped_blocks(rows or [])}

        def _balance(self, db, address):
            if not essentials.address_validate(address):
                raise _BadRequest("invalid address")
            # O(1) read from the maintained balance index when enabled; else the authoritative memoized
            # scan. The index is DISPLAY-only (consensus uses ledger_balance3), so a stale/wrong index
            # can never enable spending.
            bi = getattr(node, "balance_index", None)
            if bi is not None:
                balance = bi.get_balance(address)
            else:
                balance = db.balance_get(address)  # authoritative, memoized per chain height
            return {"address": address, "balance": str(quantize_eight(balance))}

        def _transaction(self, db, txid):
            rows = db.fetchall(db.h, "SELECT * FROM transactions WHERE signature LIKE ? LIMIT 1",
                               (txid + "%",))
            if not rows:
                raise _NotFound("no transaction matching {}".format(txid))
            return essentials.format_raw_tx(rows[0])

        def _address_txs(self, db, address, query):
            if not essentials.address_validate(address):
                raise _BadRequest("invalid address")
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                raise _BadRequest("limit must be an integer")
            limit = max(1, min(limit, 500))
            # Newest N where the address is sender OR recipient, WITHOUT sorting every matching row:
            # two index-ordered subqueries (using the (party, block_height) composite indexes from
            # db_migrations v2) each stop at LIMIT, then we merge. UNION dedups a self-send that matches
            # both sides. Falls back fine on the old OR plan if the composite indexes aren't built yet.
            rows = db.fetchall(db.h,
                               "SELECT * FROM ("
                               "  SELECT * FROM (SELECT * FROM transactions WHERE address = ? "
                               "                 ORDER BY block_height DESC LIMIT ?) "
                               "  UNION "
                               "  SELECT * FROM (SELECT * FROM transactions WHERE recipient = ? "
                               "                 ORDER BY block_height DESC LIMIT ?) "
                               ") ORDER BY block_height DESC LIMIT ?",
                               (address, limit, address, limit, limit))
            return {"address": address, "limit": limit,
                    "transactions": self._block_rows_to_json(rows or [])}

        def _mempool(self):
            rows = mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND) if mp.MEMPOOL else []
            return {"count": len(rows),
                    "transactions": [dict(zip(MEMPOOL_FIELDS, row)) for row in rows]}

    return Handler


class BismuthRESTServer(threading.Thread):
    """Daemon thread hosting the read-only REST API."""

    def __init__(self, node, host="0.0.0.0", port=5659):
        threading.Thread.__init__(self, name="RESTAPIThread")
        self.daemon = True
        self.node = node
        self.host = host
        self.port = int(port)
        self.httpd = None

    def run(self):
        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port), _make_handler(self.node))
            self.httpd.daemon_threads = True
            self.node.logger.app_log.warning(f"REST API listening on {self.host}:{self.port}")
            self._warm_supply_cache()
            self.httpd.serve_forever()
        except Exception as e:
            self.node.logger.app_log.warning(f"REST API failed to start: {e}")

    def _warm_supply_cache(self):
        """Preload the persisted circulating-supply cache so the first /api/supply after a restart is
        instant (only the cheap incremental top-up runs). If there's no cache file yet (first-ever run),
        proactively kick the background full scan so it completes ahead of demand instead of on the
        first request. Best-effort: never blocks startup or raises."""
        try:
            intmode = bool(getattr(self.node, "ledger_integer_amounts", False))
            if load_supply_cache(self.node, intmode) is None:
                start_supply_compute(self.node)
        except Exception as e:
            try:
                self.node.logger.app_log.warning("supply cache warm-up skipped: {}".format(e))
            except Exception:
                pass

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
