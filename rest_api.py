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
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            query = parse_qs(parsed.query)
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
                    return self._write(200, self._blocks_range(db, route[2], route[3]))
                if route[:1] == ["balance"] and len(route) == 2:
                    return self._write(200, self._balance(db, route[1]))
                if route[:1] == ["transaction"] and len(route) >= 2:
                    # a txid is a base64 signature prefix and may contain "/", so rejoin the tail
                    return self._write(200, self._transaction(db, "/".join(route[1:])))
                if route[:1] == ["address"] and len(route) == 3 and route[2] == "transactions":
                    return self._write(200, self._address_txs(db, route[1], query))
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
                    "endpoints": {
                        "/api/status": "node status (height, peers, difficulty, consensus)",
                        "/api/capabilities": "REST/transport capabilities for peer sync (reachable = capable)",
                        "/api/difficulty": "current difficulty detail",
                        "/api/block/height/{n}": "block at a given height",
                        "/api/block/hash/{hash}": "block by hash",
                        "/api/blocks/since/{height}": "positive-height blocks after {height} (?limit=N, parallel sync)",
                        "/api/blocks/range/{start}/{end}": "blocks in the inclusive height range (parallel sync)",
                        "/api/balance/{address}": "confirmed balance of an address",
                        "/api/transaction/{txid}": "transaction by id (signature prefix)",
                        "/api/address/{address}/transactions": "recent txs for an address (?limit=N, max 500)",
                        "/api/mempool": "pending (unconfirmed) transactions",
                        "/api/peers": "known peers",
                    }}

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

        def _blocks_range(self, db, raw_start, raw_end):
            try:
                start, end = int(raw_start), int(raw_end)
            except ValueError:
                raise _BadRequest("start and end must be integers")
            if end < start:
                raise _BadRequest("end must be >= start")
            end = min(end, start + 1000)  # cap the span
            rows = db.fetchall(db.h, "SELECT * FROM transactions WHERE block_height >= ? AND block_height <= ? "
                                     "ORDER BY block_height ASC", (start, end))
            return {"start": start, "end": end, "blocks": self._grouped_blocks(rows or [])}

        def _balance(self, db, address):
            if not essentials.address_validate(address):
                raise _BadRequest("invalid address")
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
            rows = db.fetchall(db.h, "SELECT * FROM transactions WHERE address = ? OR recipient = ? "
                                     "ORDER BY block_height DESC LIMIT ?", (address, address, limit))
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
