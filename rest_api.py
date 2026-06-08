"""
Modern, parallel REST/JSON API for Bismuth nodes (read-only, v1).

This runs in its own ``ThreadingHTTPServer`` thread *alongside* — not replacing — the legacy
length-prefixed-JSON socket protocol, so existing peers and wallets keep working unchanged. Unlike
that protocol's serial request/response pipeline over a single socket, every HTTP request here is
served concurrently in its own thread, each with its own short-lived DB handler.

Enable it in config.txt:

    rest_api=True
    rest_api_port=5659      # optional; defaults to 5659 (regnet/testnet pick their own in node.py)

It is disabled by default. v1 is strictly read-only (GET), so it cannot affect consensus; writes
(transaction submission) are intentionally left to a later, authenticated version.

Endpoints:
    GET /api                                  - this index
    GET /api/status                           - node status
    GET /api/difficulty                       - current difficulty
    GET /api/block/height/{n}                 - block at a height
    GET /api/block/hash/{hash}                - block by hash
    GET /api/balance/{address}                - address balance
    GET /api/transaction/{txid}               - transaction by id (signature prefix)
    GET /api/address/{address}/transactions   - recent txs for an address (?limit=N, max 500)
    GET /api/mempool                          - pending transactions
    GET /api/peers                            - known peers
"""
import json
import threading
import time
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
                if route == ["tokens"]:
                    return self._write(200, self._tokens(db))
                if route[:1] == ["token"] and len(route) == 2:
                    return self._write(200, self._token(db, route[1]))
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
                    }}

        def _fork(self, db):
            import fork
            tip = node.hdd_block
            reader = fork.db_fork_signal_reader(db)
            return fork.fork_status(reader, tip,
                                    getattr(node, "fork_window", fork.FORK2_WINDOW),
                                    getattr(node, "fork_boundary", fork.FORK2_BOUNDARY),
                                    getattr(node, "fork_bury", fork.FORK2_BURY))

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

        def _supply(self, db):
            """Circulating supply, computed from the ledger and cached on the node, updated INCREMENTALLY
            as the tip advances (no full rescan per call). circulating = mining emission (sum(reward) -
            sum(fee) over positive heights) + the concluded dev/HN reward mirrors (negative heights)."""
            height = int(getattr(node, "hdd_block", 0) or 0)
            intmode = bool(getattr(node, "ledger_integer_amounts", False))

            def _val(raw):
                return amounts.to_decimal(int(raw or 0)) if intmode else quantize_eight(raw or 0)

            def _emit(where, params=()):
                r = db.fetchall(db.h, "SELECT COALESCE(SUM(reward),0)-COALESCE(SUM(fee),0) "
                                      "FROM transactions WHERE " + where, params)[0][0]
                return _val(r)

            cache = getattr(node, "_supply_cache", None)
            if cache is None or cache.get("intmode") != intmode or height < cache["height"]:
                mirror = db.fetchall(db.h, "SELECT COALESCE(SUM(amount),0) FROM transactions "
                                           "WHERE block_height < 0")[0][0]
                circ = _emit("block_height >= 0") + _val(mirror)
                cache = {"height": height, "circ": circ, "intmode": intmode}
            elif height > cache["height"]:
                circ = cache["circ"] + _emit("block_height > ? AND block_height <= ?",
                                             (cache["height"], height))
                cache = {"height": height, "circ": circ, "intmode": intmode}
            node._supply_cache = cache
            return {"height": height, "circulating": str(quantize_eight(cache["circ"]))}

        def _tokens(self, db):
            """All tokens seen on chain, ranked by transfer volume."""
            rows = db.fetchall(db.index_cursor, "SELECT token, COUNT(*) FROM tokens "
                                                "GROUP BY token ORDER BY COUNT(*) DESC LIMIT 500")
            return {"count": len(rows), "tokens": [{"token": r[0], "transfers": r[1]} for r in rows]}

        def _token(self, db, name):
            """A token's holders, per-address balances, and supply (credits - debits, token index)."""
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
            return {"token": name, "supply": sum(h["balance"] for h in holders),
                    "holder_count": len(holders), "transfers": transfers, "holders": holders[:200]}

        def _block_rows_to_json(self, rows):
            return [essentials.format_raw_tx(row) for row in rows]

        def _block_by_height(self, db, raw_height):
            try:
                height = int(raw_height)
            except ValueError:
                raise _BadRequest("height must be an integer")
            rows = db.fetchall(db.h, "SELECT * FROM transactions WHERE block_height = ?", (height,))
            if not rows:
                raise _NotFound("no block at height {}".format(height))
            return {"block_height": height, "transactions": self._block_rows_to_json(rows)}

        def _block_by_hash(self, db, block_hash):
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
            rows = db.fetchall(db.h, "SELECT * FROM transactions WHERE block_height > ? AND block_height <= ? "
                                     "ORDER BY block_height ASC", (since, since + limit))
            return {"since": since, "limit": limit, "blocks": self._grouped_blocks(rows or [])}

        def _blocks_range(self, db, raw_start, raw_end, query=None):
            try:
                start, end = int(raw_start), int(raw_end)
            except ValueError:
                raise _BadRequest("start and end must be integers")
            if end < start:
                raise _BadRequest("end must be >= start")
            end = min(end, start + 1000)  # cap the span
            rows = db.fetchall(db.h, "SELECT * FROM transactions WHERE block_height >= ? AND block_height <= ? "
                                     "ORDER BY block_height ASC", (start, end))
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
            self.httpd.serve_forever()
        except Exception as e:
            self.node.logger.app_log.warning(f"REST API failed to start: {e}")

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
