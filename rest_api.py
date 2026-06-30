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
import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import amounts
import bismuth_serialize
import dbhandler
import essentials
import mempool as mp
import transport
from quantizer import quantize_eight

__version__ = "0.1.0"

MEMPOOL_FIELDS = ("timestamp", "address", "recipient", "amount",
                  "signature", "public_key", "operation", "openfield")

PROXY_TIMEOUT = 10            # seconds to wait on the upstream node when relaying
PROXY_MAX_BYTES = 8_000_000   # cap on a relayed response body (a RingCT spend block is large but bounded)
# Bismuth REST is conventionally :5659 (mainnet) / :5658 socket; 80/443 cover plain explorers. Restricting
# the relay to these (+ the node's own REST port) stops the proxy being used as an arbitrary-port scanner /
# response oracle (audit M-1) while keeping the real use case — browsing a peer node's :5659 /api — working.
PROXY_DEFAULT_PORTS = (80, 443, 5658, 5659)
PROXY_MAX_CONCURRENCY = 8     # in-flight relayed fetches, total (audit M-2: bound the amplification surface)
PROXY_RATE_MAX = 30          # per-client-IP relayed requests allowed per window (audit M-2)
PROXY_RATE_WINDOW = 10       # seconds
PROXY_MAX_REDIRECTS = 0      # the relay NEVER follows redirects (audit H-2): a 3xx to an internal host would
                            # bypass the SSRF guard, which only validates the initial target

# Bounded concurrency + a coarse per-IP token bucket for /api/proxy, shared across all handler threads.
_PROXY_SEM = threading.BoundedSemaphore(PROXY_MAX_CONCURRENCY)
_proxy_rl_lock = threading.Lock()
_proxy_rl = {}               # client_ip -> deque-ish list of recent request timestamps


def _proxy_rate_ok(ip):
    """Coarse per-IP rate limit for the relay: at most PROXY_RATE_MAX requests per PROXY_RATE_WINDOW
    seconds. Keeps a bounded timestamp list per active IP and prunes stale ones opportunistically."""
    now = time.time()
    cutoff = now - PROXY_RATE_WINDOW
    with _proxy_rl_lock:
        q = _proxy_rl.get(ip)
        if q is None:
            q = []
            _proxy_rl[ip] = q
        while q and q[0] < cutoff:
            q.pop(0)
        if len(q) >= PROXY_RATE_MAX:
            return False
        q.append(now)
        if len(_proxy_rl) > 4096:                       # don't let the table grow unbounded
            for k in [k for k, v in _proxy_rl.items() if not v or v[-1] < cutoff]:
                _proxy_rl.pop(k, None)
        return True


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
            # A bytes payload is a pre-encoded BINARY body (e.g. the compact sync_codec sync format) sent
            # as application/octet-stream; anything else is JSON. Both still get HTTP gzip negotiation.
            if isinstance(payload, (bytes, bytearray)):
                body = bytes(payload)
                ctype = "application/octet-stream"
            else:
                body = json.dumps(payload).encode("utf-8")
                ctype = "application/json"
            # Transport compression at the HTTP layer (standard Content-Encoding) — this is where
            # bandwidth savings for parallel block fetching live, not in the legacy socket protocol.
            http_enc, codec = self._negotiate_encoding()
            if codec:
                body = transport.compress(codec, body)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
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

        def _snapshot_info(self):
            # doc/43: manifest of the pre-built snapshot this node serves (or {available:false}). Reading
            # the sidecar only; never scans the live ledger. Gated by snapshot_serve.
            if not getattr(node, "snapshot_serve", False):
                raise _NotFound("snapshot serving disabled")
            import snapshot_p2p
            man = snapshot_p2p.read_manifest(getattr(node, "snapshot_path", "") or "static/ledger-snapshot.tar.gz")
            return {"available": True, **man} if man else {"available": False}

        def _send_snapshot(self):
            # doc/43: stream the ALREADY-BUILT snapshot tarball (chunked; tar.gz is already compressed, so
            # no HTTP re-compression). Never builds/scans on a request.
            if not getattr(node, "snapshot_serve", False):
                raise _NotFound("snapshot serving disabled")
            import os, snapshot_p2p
            path = getattr(node, "snapshot_path", "") or "static/ledger-snapshot.tar.gz"
            man = snapshot_p2p.read_manifest(path)
            if not man:
                raise _NotFound("no snapshot available")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.send_header("X-Bismuth-Snapshot-Height", str(man["height"]))
            self.send_header("X-Bismuth-Snapshot-Sha256", man["sha256"])
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    self.wfile.write(chunk)

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
                if route == ["snapshot", "info"]:
                    return self._write(200, self._snapshot_info())
                if route == ["snapshot"]:
                    return self._send_snapshot()
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
                if route == ["proxy"]:
                    # Same-origin relay (no DB): lets an HTTPS-hosted explorer browse an HTTP peer node
                    # without the browser's mixed-content block. Propagates the upstream status code.
                    status, payload = self._proxy(query)
                    return self._write(status, payload)

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
                    return self._write(200, self._transaction(db, "/".join(route[1:]), query))
                if route[:1] == ["address"] and len(route) == 3 and route[2] == "transactions":
                    return self._write(200, self._address_txs(db, route[1], query))
                if route == ["fork"]:
                    return self._write(200, self._fork(db))
                if route[:1] == ["pubkey"] and len(route) == 2:
                    return self._write(200, self._pubkey(route[1]))
                if route == ["supply"]:
                    return self._write(200, self._supply(db))
                if route[:1] == ["stats"] and len(route) == 2:
                    return self._write(200, self._stats(db, route[1], query))
                # Poker accounts + leaderboard (doc/28 §7, Stage 4). READ-ONLY, consensus-neutral: a pure fold
                # over the poker tables' append-only TAG_RESULT log read from CONTRACT STATE (never the
                # ledger). /api/poker/leaderboard, /api/poker/account/<addr>, /api/poker/table/<addr>/results.
                if route[:1] == ["poker"] and len(route) >= 2:
                    return self._write(200, self._poker(route[1:], query))
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
                if route[:2] == ["token", "tx"] and len(route) == 3:
                    return self._write(200, self._token_tx(db, route[2], query))
                if route[:1] == ["token"] and len(route) == 2:
                    return self._write(200, self._token(db, route[1], query))
                if route[:1] == ["alias"] and len(route) == 2:
                    return self._write(200, self._alias_lookup(db, route[1]))
                if route[:1] == ["aliases"] and len(route) == 2:
                    return self._write(200, self._aliases_for_address(db, route[1]))
                raise _NotFound("unknown endpoint")
            except _NotFound as e:
                self._write(404, {"error": "not_found", "detail": str(e)})
            except _BadRequest as e:
                self._write(400, {"error": "bad_request", "detail": str(e)})
            except _Forbidden as e:
                self._write(403, {"error": "forbidden", "detail": str(e)})
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
                if route == ["block"]:
                    if not getattr(node, "rest_api_write", False):
                        raise _Forbidden("block submission is disabled; start the node with rest_api_write=True")
                    payload = self._read_body_json()
                    db = self._db()
                    return self._write(200, self._submit_block(db, payload))
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
            # hf2: operation/openfield are uncapped post-fork, so this is a TRANSPORT-level DoS guard (bound
            # the in-memory body read/JSON parse), NOT a consensus field cap — the socket path is unbounded
            # and the per-byte openfield fee is the real economic backpressure. Generous enough for large
            # free-form KV payloads.
            if n <= 0 or n > 64_000_000:
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
            # Echo each tx's canonical id: post-hf2 (the next block is at/after fork_height) it's the
            # content-hash txid; otherwise the legacy signature[:56] slice.
            fh = getattr(node, "fork_height", None)
            post_fork = fh is not None and (int(getattr(node, "last_block", 0)) + 1) >= int(fh)
            # Stage 2 (doc/29): post-fork the canonical id is the BINARY-pre-image content txid; tx_id_v2_s
            # normalises the wire ts/amount to canonical centiseconds/units (also fixes audit L-3 — the echo
            # no longer hashes the raw, possibly non-'%.8f', wire amount).
            txids = [bismuth_serialize.tx_id_v2_s(tx[0], tx[1], tx[2], tx[3], tx[6], tx[7]) if post_fork
                     else tx[4][:56] for tx in txs]
            return {"submitted": len(txs), "txids": txids, "result": result}

        def _submit_block(self, db, payload):
            """Submit a MINED block over REST — the consensus-neutral REST transport for the legacy socket
            'block' command (post-hf2 mining moves off the socket; doc/39, issue #380). It routes the block
            through the IDENTICAL digest_block path the socket handler uses (NO new validation logic) and
            applies the SAME guards as node.py's socket 'block' handler. Gated by rest_api_write."""
            from digest import digest_block
            segments = payload.get("block") if isinstance(payload, dict) else payload
            if not (isinstance(segments, list) and segments):
                raise _BadRequest("provide 'block': the mined block (a list of one block = a list of "
                                  "tx arrays incl. the signed coinbase), exactly as the socket 'block' command")
            peer_ip = self.client_address[0] if self.client_address else "127.0.0.1"
            if not node.peers.is_allowed(peer_ip, "block"):
                raise _Forbidden("%s is not allowed for block submission (see the 'allowed'/whitelist config)" % peer_ip)
            # mirror node.py's socket 'block' mainnet guards exactly
            if node.is_mainnet:
                if len(node.peers.connection_pool) < 5 and not node.peers.is_whitelisted(peer_ip):
                    raise _BadRequest("insufficient connections to the network")
                if node.db_lock.locked():
                    raise _BadRequest("node is digesting; retry shortly")
                if node.last_block < node.peers.consensus_max - 3:
                    raise _BadRequest("node not synced (at %s, need >= %s); block would orphan"
                                      % (node.last_block, node.peers.consensus_max - 3))
            try:
                # sdef is vestigial in the digest path (peers.warning ignores it); pass None.
                digest_block(node, segments, None, peer_ip, db)
            except ValueError as e:
                return {"accepted": False, "reason": str(e)}
            return {"accepted": True, "block_height": node.last_block, "block_hash": node.last_block_hash}

        # --- helpers -------------------------------------------------------
        def _db(self):
            return dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                                       node.ledger_ram_file, node.logger,
                                       trace_db_calls=node.trace_db_calls)

        @staticmethod
        def _check_key(value, what="parameter", maxlen=256):
            """Reject an empty / over-long path parameter early (audit L-10) so a malformed token/alias/
            address lookup is a cheap 400 instead of a wasted DB round-trip. (Queries are already bound +
            LIMITed, so this is hardening/consistency, not an injection fix.)"""
            if not value or len(value) > maxlen:
                raise _BadRequest("%s missing or too long (max %d chars)" % (what, maxlen))
            return value

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
                        "/api/stats/summary": "network dashboard: height, difficulty, avg block time, peers, mempool, tokens",
                        "/api/stats/monthly": "per-month tx count, volume, fees, coin emission, cumulative supply, active addresses (background-cached, incremental)",
                        "/api/stats/tx_per_month": "transactions-per-month histogram (served from the monthly cache)",
                        "/api/stats/new_addresses": "newly-created addresses per month (background-cached, incremental)",
                        "/api/stats/rich_list": "top addresses by balance (background-cached, incremental); ?top=N (<=500)",
                        "/api/stats/top_miners": "mining distribution: blocks + reward per miner, share% (background-cached); ?top=N",
                        "/api/stats/largest_txs": "largest transactions by amount (background-cached); ?top=N (<=100)",
                        "/api/stats/market": "price / market cap / 24h volume (coingecko, TTL-cached; rest_api_market to disable)",
                        "/api/stats/difficulty": "difficulty time-series sampled from the misc table",
                        "/api/stats/geo": "geolocated peers for the explorer node map (cached; rest_api_geo to disable)",
                        "/api/poker/leaderboard": "poker accounts ranked by ?metric=net|won|hands_won|hands_played|biggest_pot&top=N (off-chain fold over contract state; background-cached, incremental)",
                        "/api/poker/account/{address}": "one player's poker stats keyed by full 28-byte address: hands played/won, net chips, biggest pot, tables seen",
                        "/api/poker/table/{address}/results": "a poker table's append-only result log (?since=&limit=); read from contract state, never the ledger",
                        "/api/tokens": "all tokens on chain, ranked by transfer volume",
                        "/api/token/{name}": "a token's supply, holder count, and per-address balances",
                        "/api/token/tx/{address}": "token transfers (sent or received) for an address, newest "
                                                   "first; each {token, block_height, timestamp, sender, "
                                                   "recipient, amount, signature}",
                        "/api/alias/{name}": "resolve an alias to its owner address: {alias, address}",
                        "/api/aliases/{address}": "all aliases owned by an address: {address, count, aliases}",
                        "/api/fork": "hf2 auto-fork readiness: signalling run, lock-in, activation height",
                        "/api/pubkey/{address}": "hf2 pubkey-by-reference: is the address's key registered (safe to omit on the wire)",
                        "/api/shield/stats": "shielded pool (doc/22): note/nullifier counts, pool value, sink",
                        "/api/shield/note/{note_id}": "public fields of a shielded note (nothing decryptable)",
                        "POST /api/transaction": "submit a signed tx (needs rest_api_write=True); body "
                                                 "{\"transaction\": [ts, address, recipient, amount, "
                                                 "signature, public_key, operation, openfield]} — the "
                                                 "post-hardfork submission path, no socket needed",
                        "/api/proxy?target={url}": "same-origin relay to another node's read-only /api, so "
                                                   "an HTTPS-hosted explorer can browse an HTTP node without "
                                                   "the browser's mixed-content block (read-only, GET-only, "
                                                   "/api paths to public hosts; disable with rest_api_proxy=False)",
                    }}

        @staticmethod
        def _proxy_allowed_ports():
            """The ports the relay may target: the standard Bismuth REST/socket ports + 80/443, plus this
            node's own REST port, plus any operator-configured `rest_api_proxy_ports`. Restricting to these
            stops the proxy being used as an arbitrary-port scanner / service oracle (audit M-1)."""
            ports = set(PROXY_DEFAULT_PORTS)
            rp = getattr(node, "rest_api_port", None)
            if rp:
                try:
                    ports.add(int(rp))
                except (TypeError, ValueError):
                    pass
            extra = getattr(node, "rest_api_proxy_ports", None)
            if extra:
                for p in str(extra).replace(",", " ").split():
                    try:
                        ports.add(int(p))
                    except ValueError:
                        pass
            return ports

        @staticmethod
        def _proxy_guard_host(host):
            """SSRF guard. Resolve `host` ONCE, require EVERY resolved address to be public (refuse
            loopback / private / link-local incl. the 169.254.169.254 cloud-metadata IP / multicast /
            reserved / unspecified), and RETURN one validated public IP to connect to. Pinning that IP for
            the actual fetch closes the DNS-rebind TOCTOU (audit H-3): the guard's lookup and the connection
            can no longer resolve to different addresses, because there is exactly one lookup."""
            try:
                infos = socket.getaddrinfo(host, None)
            except Exception as e:
                raise _BadRequest("proxy target does not resolve: %s" % e)
            chosen = None
            for info in infos:
                addr = info[4][0].split("%")[0]   # strip any IPv6 zone id
                try:
                    ip = ipaddress.ip_address(addr)
                except ValueError:
                    raise _Forbidden("proxy target resolves to a non-IP address")
                if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                        or ip.is_reserved or ip.is_unspecified):
                    raise _Forbidden("proxy target resolves to a non-public address (%s)" % ip)
                if chosen is None:
                    chosen = addr
            if chosen is None:
                raise _BadRequest("proxy target does not resolve")
            return chosen

        def _proxy_fetch(self, scheme, host, ip, port, path_qs):
            """Fetch `path_qs` from the validated, PINNED public IP (closes DNS-rebind H-3), REFUSING any
            redirect (closes the redirect SSRF bypass H-2 — a 3xx Location to an internal host would never
            be re-validated). For https the TLS handshake still uses the hostname for SNI + certificate
            validation while the underlying socket connects to the pinned IP. Returns (status, raw bytes)."""
            headers = {"Accept": "application/json", "User-Agent": "bismuth-explorer-proxy",
                       "Connection": "close"}     # no Accept-Encoding -> upstream replies plaintext JSON
            if scheme == "https":
                raw_sock = socket.create_connection((ip, port), PROXY_TIMEOUT)
                try:
                    tls = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=host)
                except Exception:
                    raw_sock.close()
                    raise
                conn = http.client.HTTPSConnection(host, port, timeout=PROXY_TIMEOUT)
                conn.sock = tls
            else:
                conn = http.client.HTTPConnection(host, port, timeout=PROXY_TIMEOUT)
                conn.sock = socket.create_connection((ip, port), PROXY_TIMEOUT)
            try:
                conn.request("GET", path_qs, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                if status in (301, 302, 303, 307, 308):
                    raise _Forbidden("proxy does not follow redirects (target returned %d)" % status)
                return status, resp.read(PROXY_MAX_BYTES + 1)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        def _proxy(self, query):
            """Same-origin relay so an HTTPS-hosted explorer (explorer.bismuth.cz) can browse an HTTP peer
            node despite the browser's mixed-content policy. The browser only ever talks to THIS node (its
            own HTTPS origin via nginx); this node fetches the *target* node's read-only ``/api`` itself
            (node->node HTTP — no browser policy involved) and returns the JSON. Read-only and locked down:
            GET only, target scheme must be http(s), target path must be under ``/api``, the target PORT
            must be allowed, and the host must resolve to a public address — to which we PIN the connection
            (no rebind) and from which we refuse redirects (no SSRF pivot). Per-IP rate-limited and globally
            concurrency-capped. Returns ``(status, payload)`` so an upstream 404 propagates to the explorer
            instead of masquerading as a 200."""
            if not getattr(node, "rest_api_proxy", True):
                raise _Forbidden("node-browsing relay is disabled (set rest_api_proxy=True to enable)")
            client_ip = self.client_address[0] if self.client_address else "?"
            if not _proxy_rate_ok(client_ip):
                return 429, {"error": "rate_limited", "detail": "too many relay requests; slow down"}
            target = (query.get("target") or [None])[0]
            if not target:
                raise _BadRequest("proxy requires ?target=<node /api url>")
            u = urlparse(target)
            if u.scheme not in ("http", "https"):
                raise _BadRequest("proxy target must be an http(s) URL")
            if not u.hostname:
                raise _BadRequest("proxy target has no host")
            if not (u.path == "/api" or u.path.startswith("/api/")):
                raise _Forbidden("proxy only relays /api paths")  # never an arbitrary-URL fetcher
            port = u.port or (443 if u.scheme == "https" else 80)
            if port not in self._proxy_allowed_ports():
                raise _Forbidden("proxy target port %d is not allowed" % port)
            ip = self._proxy_guard_host(u.hostname)          # one resolution -> validated public IP, pinned
            path_qs = u.path + (("?" + u.query) if u.query else "")
            if not _PROXY_SEM.acquire(blocking=False):
                return 503, {"error": "proxy_busy", "detail": "relay at capacity; retry shortly"}
            try:
                status, raw = self._proxy_fetch(u.scheme, u.hostname, ip, port, path_qs)
            except _Forbidden:
                raise                                         # redirect refusal -> 403
            except Exception as e:                            # DNS/connection/timeout/TLS — generic (no oracle)
                try:
                    node.logger.app_log.warning("proxy fetch failed (%s): %s" % (u.hostname, e))
                except Exception:
                    pass
                return 502, {"error": "proxy_unreachable"}
            finally:
                _PROXY_SEM.release()
            if len(raw) > PROXY_MAX_BYTES:
                return 502, {"error": "proxy_too_large"}
            try:
                return status, json.loads(raw or b"null")
            except Exception:
                return 502, {"error": "proxy_bad_json"}

        def _pubkey(self, address):
            """hf2 pubkey-by-reference (doc/40): whether ``address`` already has a public key registered
            from a prior confirmed block — so a producer (wallet / pool) knows it can safely OMIT the key
            on the wire (``public_key=""``) and have the node resolve it by address. ``registered=false``
            also when the registry is not built (pre-fork / flag off), so the caller must carry the key
            INLINE (fail-closed)."""
            reg = getattr(node, "pk_registry", None)
            if reg is None:
                return {"address": address, "registered": False, "available": False}
            fseen = reg.first_seen(address)
            return {"address": address, "registered": fseen is not None,
                    "available": True, "first_height": fseen}

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
            # doc/41: the fork signal moved coinbase slots at activation. BELOW fork_height it rides in the
            # openfield (legacy; the nonce/root hex there can't false-match 'hf2'); AT/AFTER fork_height the
            # coinbase openfield is free miner data and the signal rides in the public_key commitment
            # ("hf2"+"vmsr"<root>) — so match per-era to avoid both misses (post-fork) and false hits (a
            # pre-fork RSA b64 pubkey can contain 'hf2'; free post-fork openfield can too). Pre-activation
            # (fork_height None) every block is pre-fork -> openfield only, byte-identical to before.
            _ff = getattr(node, "fork_height", None)
            _cb_join = ("SELECT t.block_height FROM transactions t "
                        "JOIN (SELECT block_height, MAX(rowid) mr FROM transactions "
                        "      WHERE block_height >= 1 AND block_height <= ? GROUP BY block_height) m "
                        "ON t.rowid = m.mr WHERE ")
            _sig = "%" + fork.FORK2_SIGNAL + "%"
            if _ff is None:
                db.execute_param(db.c, _cb_join + "t.openfield LIKE ?", (int(tip), _sig))
            else:
                db.execute_param(db.c, _cb_join +
                                 "((t.block_height < ? AND t.openfield LIKE ?) "
                                 " OR (t.block_height >= ? AND t.public_key LIKE ?))",
                                 (int(tip), int(_ff), _sig, int(_ff), _sig))
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
            connected_raw = set(getattr(p, "_connection_pool_set", set()) or [])

            # The live connection pool is keyed by host:port, but opinions / versions / reputation are all
            # keyed by bare host. Unioning them verbatim listed every connected peer TWICE — once as
            # "host:port" (connected, but height/version/reputation all blank) and once as bare "host"
            # (known, with the real data). Normalize the connection set to bare host so each peer is one
            # row carrying both its connected flag and its height/version/reputation.
            def _host(x):
                s = str(x)
                head, sep, tail = s.rpartition(":")
                return head if (sep and tail.isdigit() and head.count(":") == 0) else s  # strip IPv4 :port only
            connected = {_host(c) for c in connected_raw}
            connected |= {_host(c) for c in (getattr(p, "connection_pool", []) or [])}
            # FULL peer set, not just live connections: the KNOWN set (peer_dict from peers.txt /
            # suggested_peers.txt — every node we've learned about / talked to) and the banlist, mirroring
            # rest_stats._peer_status / the geomap. Without this the list only showed the handful of live
            # connections while the map showed everyone; now both paint the whole network the node is aware of.
            known = {_host(ip) for ip in (getattr(p, "peer_dict", {}) or {})}
            banned = {_host(ip) for ip in (getattr(p, "banlist", []) or [])}
            tip = int(getattr(node, "hdd_block", 0) or 0)
            out = []
            counts = {"connected": 0, "known": 0, "banned": 0}
            for ip in (set(opinions) | set(versions) | connected | known | banned):
                if not ip:
                    continue
                h = opinions.get(ip)
                is_banned = (ip in banned) or (p.is_banned(ip) if hasattr(p, "is_banned") else False)
                is_conn = ip in connected
                # precedence matches _peer_status: banned > connected > merely known/seen
                status = "banned" if is_banned else ("connected" if is_conn else "known")
                counts[status] += 1
                out.append({
                    "ip": ip,
                    "height": h,
                    "behind": (tip - h) if isinstance(h, int) else None,
                    "version": versions.get(ip),
                    "reputation": p.reputation(ip) if hasattr(p, "reputation") else 0,
                    "connected": is_conn,
                    "banned": is_banned,
                    "whitelisted": p.is_whitelisted(ip) if hasattr(p, "is_whitelisted") else False,
                    "status": status,
                })
            # connected first, then known (by reputation/height), banned last
            _rank = {"connected": 2, "known": 1, "banned": 0}
            out.sort(key=lambda n: (_rank[n["status"]], n["reputation"], n["height"] or 0), reverse=True)
            return {"count": len(out), "tip": tip,
                    "consensus": getattr(p, "consensus", None),
                    "consensus_percentage": getattr(p, "consensus_percentage", None),
                    "status_counts": counts,
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

        def _stats(self, db, which, query=None):
            """Explorer statistics (doc/15). Heavy aggregates (tx-per-month, new-addresses, rich-list) are
            background-cached on the node and topped up incrementally; difficulty is sampled from the indexed
            misc table; geo is best-effort + cached. All read-only / consensus-neutral."""
            import rest_stats
            query = query or {}
            def _top(default):
                try:
                    return int((query.get("top") or [str(default)])[0])
                except (TypeError, ValueError):
                    return default
            if which == "summary":
                return rest_stats.network_summary(node, db)
            if which == "monthly":
                return rest_stats.monthly(node, db)
            if which in ("tx_per_month", "txmonth"):
                return rest_stats.tx_per_month(node, db)
            if which in ("new_addresses", "newaddr"):
                return rest_stats.new_addresses(node, db)
            if which in ("rich_list", "richlist", "rich"):
                return rest_stats.rich_list(node, db, top=_top(100))
            if which in ("top_miners", "miners"):
                return rest_stats.top_miners(node, db, top=_top(50))
            if which in ("largest_txs", "largest", "bigtx"):
                return rest_stats.largest_txs(node, db, top=_top(25))
            if which == "market":
                return rest_stats.market(node)
            if which == "difficulty":
                tip = int(getattr(node, "hdd_block", 0) or 0)
                return {"tip": tip, "series": rest_stats.difficulty_series(db, tip)}
            if which == "geo":
                return rest_stats.geo_nodes(node)
            raise _NotFound("unknown stats endpoint (summary|monthly|tx_per_month|new_addresses|"
                            "rich_list|top_miners|largest_txs|market|difficulty|geo)")

        def _poker(self, route, query=None):
            """Poker accounts + leaderboard (doc/28 §7, Stage 4). READ-ONLY + consensus-neutral. poker_stats.py
            folds the poker tables' append-only TAG_RESULT log read from CONTRACT STATE (node.vm_state, NOT
            the ledger) into a per-account view; a per-ledger JSON cache (namespaced by ledger filename) is
            advanced incrementally by a single guarded background daemon — the request path never scans.
              GET /api/poker/leaderboard?metric=net|won|hands_won|hands_played|biggest_pot&top=N
              GET /api/poker/account/<address>            (full 28-byte hex [SEC-C1])
              GET /api/poker/table/<addr>/results?since=&limit=
            """
            import poker_stats
            q = query or {}

            def _q(name, default=None):
                v = q.get(name)
                if isinstance(v, list):
                    v = v[0] if v else None
                return v if v is not None else default

            def _int(name, default, hi=None):
                try:
                    n = int(_q(name, default))
                except (TypeError, ValueError):
                    n = int(default)
                if hi is not None:
                    n = min(n, hi)
                return max(0, n)

            if route == ["leaderboard"]:
                metric = _q("metric", "net")
                return poker_stats.request_leaderboard(node, metric=metric, top=_int("top", 50, 500))
            if route[:1] == ["account"] and len(route) == 2:
                return poker_stats.request_account(node, route[1])
            if len(route) == 3 and route[0] == "table" and route[2] == "results":
                return poker_stats.request_table_results(node, route[1],
                                                         since=_int("since", 0), limit=_int("limit", 200, 1000))
            raise _NotFound("unknown poker endpoint (leaderboard|account/<addr>|table/<addr>/results)")

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

        def _token_tx(self, db, address, query):
            """Token transfers (sent OR received) for an address, newest first, each a dict
            {token, block_height, timestamp, sender, recipient, amount, signature}. Same SEAM as _tokens:
            the LMDB token index (tokens_aliases plugin) when enabled, else the legacy index.db."""
            self._check_key(address, "address", 128)
            try:
                limit = max(1, min(int(query.get("limit", ["100"])[0]), 500))
            except ValueError:
                raise _BadRequest("limit must be an integer")
            if node.token_index is not None:
                rows = node.token_index.token_txs_for_address(address, limit)
            else:
                # legacy index.db tokens table: (block_height, timestamp, token, address, recipient, txid, amount)
                r = db.fetchall(db.index_cursor,
                                "SELECT token, block_height, timestamp, address, recipient, amount, txid "
                                "FROM tokens WHERE address = ? OR recipient = ? "
                                "ORDER BY block_height DESC LIMIT ?", (address, address, limit))
                def _amt(v):                       # legacy index.db has some malformed rows (e.g. '{amount}')
                    try:
                        return int(v or 0)
                    except (ValueError, TypeError):
                        return 0
                rows = [{"token": x[0], "block_height": x[1], "timestamp": x[2], "sender": x[3],
                         "recipient": x[4], "amount": _amt(x[5]), "signature": x[6]} for x in (r or [])]
            return {"address": address, "count": len(rows), "limit": limit, "transactions": rows}

        def _alias_lookup(self, db, alias):
            """Resolve an alias to its current owner address (None if unclaimed/free). Same SEAM as the
            token endpoints: the LMDB token index (tokens_aliases plugin) when enabled, else the legacy
            index.db (first claimant)."""
            self._check_key(alias, "alias", 256)
            if node.token_index is not None:
                owner = node.token_index.alias_owner(alias)
            else:
                rows = db.fetchall(db.index_cursor,
                                   "SELECT address FROM aliases WHERE alias = ? ORDER BY block_height ASC LIMIT 1",
                                   (alias,))
                owner = rows[0][0] if rows else None
            return {"alias": alias, "address": owner}

        def _aliases_for_address(self, db, address):
            """All aliases owned by an address (registration order). LMDB token index when enabled, else
            legacy index.db."""
            self._check_key(address, "address", 128)
            if node.token_index is not None:
                aliases = node.token_index.aliases_of(address)
            else:
                rows = db.fetchall(db.index_cursor,
                                   "SELECT alias FROM aliases WHERE address = ? ORDER BY block_height ASC",
                                   (address,))
                aliases = [r[0] for r in (rows or [])]
            return {"address": address, "count": len(aliases), "aliases": aliases}

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
            fh = getattr(node, "fork_height", None)
            return [essentials.format_raw_tx(row, fh) for row in rows]

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
            fh = getattr(node, "fork_height", None)
            blocks = {}
            for row in rows:
                blocks.setdefault(row[0], []).append(essentials.format_raw_tx(row, fh))
            return [{"block_height": h, "transactions": txs} for h, txs in blocks.items()]

        def _row_to_sync_tx(self, row):
            # Consensus-faithful tx tuple in the EXACT order the digester consumes
            # (timestamp, address, recipient, amount, signature, public_key, operation, openfield).
            # Unlike format_raw_tx this keeps the public key BASE64-ENCODED as stored — decoding it (as
            # the display API does) would corrupt the bytes the signature is verified against. amount uses
            # consensus_amount (EXACT, never a float) so a REST-synced node and a socket-synced node derive
            # byte-identical blocks; display_amount here was lossy above 2**53 units and would fork the two
            # transports on any >~90M BIS tx in integer-storage mode (audit H-1).
            return [row[1], row[2], row[3], amounts.consensus_amount(row[4]),
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
            if fmt == "binary":
                # compact TRUE-BYTES sync payload (sync_codec): raw hashes, varint amounts, raw sig/pubkey
                # bytes — ~30-40% of the JSON size. Pure transport encoding: decode_blocks reconstructs the
                # byte-identical sync tuples, so the digester sees the same input as the JSON path.
                import sync_codec
                return sync_codec.encode_blocks(self._grouped_sync_blocks(rows or []))
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

        def _transaction(self, db, txid, query=None):
            fh = getattr(node, "fork_height", None)
            # Shape-dispatch (doc/18 §A.1 §6): a 64-char lowercase-hex id is a post-hf2 content-hash txid;
            # it has no signature prefix to match, so locate the post-fork tx whose content hashes to it.
            # Anything else is the legacy signature[:56] slice -> indexed prefix match (unchanged).
            if len(txid) == 64 and all(c in "0123456789abcdef" for c in txid):
                if fh is None:
                    raise _NotFound("no transaction matching {}".format(txid))
                # The content txid has NO DB column by design (computed on read), so resolve it by scanning
                # post-fork rows and re-hashing each. Make that scan BOUNDED + recent-first (audit H-4): an
                # unauthenticated lookup of a random/absent 64-hex id must not drag the ENTIRE post-fork
                # ledger through blake2b. Stream newest-first over the idx_tx_block_height range and short-
                # circuit on the first match; cap the rows scanned. A deep historical id can be reached with
                # ?from_height (move the window down); without it, the default window is the recent tip back.
                try:
                    from_h = int((query or {}).get("from_height", [0])[0])
                except (ValueError, TypeError):
                    raise _BadRequest("from_height must be an integer")
                max_scan = int(getattr(node, "txid_scan_limit", 250000))
                tip = int(getattr(node, "hdd_block", 0) or 0)
                # default lower bound: a recent window back from the tip (covers freshly-created txids, the
                # common lookup) but never below the fork height; ?from_height overrides it downward.
                default_lo = max(int(fh), tip - max_scan)
                start_h = max(int(fh), from_h) if from_h else default_lo
                db.execute_param(db.h, "SELECT * FROM transactions WHERE block_height >= ? "
                                       "ORDER BY block_height DESC", (start_h,))
                scanned = 0
                for row in db.h:
                    scanned += 1
                    if scanned > max_scan:
                        raise _BadRequest("txid scan limit (%d rows) exceeded from height %d; "
                                          "narrow the search with ?from_height=" % (max_scan, start_h))
                    tx = essentials.format_raw_tx(row, fh)
                    if tx.get("txid") == txid:
                        return tx
                raise _NotFound("no transaction matching {} at/after height {}".format(txid, start_h))
            # A bare `signature LIKE 'prefix%'` is a FULL-TABLE scan of the 23GB ledger (no plain signature
            # index exists; and LIMIT 1 does not help the not-found case — it scans to the very end). Seek
            # via the existing TXID4_Index (db_migrations.py: index on substr(signature,1,4)) for any prefix
            # >= 4 chars, exactly like digest._signature_exists_in_ledger / the mempool SQL — turning the
            # scan into an index seek + a tiny same-prefix bucket scan. Sub-4-char prefixes (rare/degenerate)
            # fall back to the legacy scan.
            if len(txid) >= 4:
                rows = db.fetchall(db.h,
                                   "SELECT * FROM transactions WHERE substr(signature,1,4)=substr(?1,1,4) "
                                   "AND signature LIKE ?2 LIMIT 1", (txid, txid + "%"))
            else:
                rows = db.fetchall(db.h, "SELECT * FROM transactions WHERE signature LIKE ? LIMIT 1",
                                   (txid + "%",))
            if not rows:
                raise _NotFound("no transaction matching {}".format(txid))
            return essentials.format_raw_tx(rows[0], fh)

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
