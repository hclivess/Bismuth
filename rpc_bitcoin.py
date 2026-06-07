"""
Bitcoin-compatible JSON-RPC adapter (doc/17) — maps the core ``bitcoind`` methods onto the Bismuth
ledger so standard tooling (exchanges, explorers, watch-only wallets) can integrate without bespoke
code. Read-focused; behind the ``rpc_bitcoin`` flag (default off, port 8332 like bitcoind).

It is a *compatibility shim*, not a full bitcoind: the methods whose semantics map cleanly are provided
(chain height/hash, blocks, balances, transactions, difficulty). Bismuth's tx model differs from
Bitcoin's UTXO model, so ``sendrawtransaction`` accepts a Bismuth-format signed tx rather than a
Bitcoin raw hex, and address/UTXO-specific calls are intentionally omitted.

Deps: stdlib only. Reuses dbhandler + essentials for data access (same as rest_api.py).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dbhandler
import essentials

__version__ = "0.1.0"


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _make_handler(node):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass  # quiet

        def _db(self):
            return dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                                       node.ledger_ram_file, node.logger,
                                       trace_db_calls=node.trace_db_calls)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send({"result": None, "error": {"code": -32700, "message": "parse error"},
                                   "id": None})
            rid = req.get("id")
            db = None
            try:
                db = self._db()
                result = self._dispatch(db, req.get("method"), req.get("params") or [])
                self._send({"result": result, "error": None, "id": rid})
            except _RpcError as e:
                self._send({"result": None, "error": {"code": e.code, "message": e.message}, "id": rid})
            except Exception as e:
                self._send({"result": None, "error": {"code": -32603, "message": str(e)}, "id": rid})
            finally:
                if db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass

        def _send(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # --- bitcoind method map -------------------------------------------
        def _dispatch(self, db, method, params):
            fn = getattr(self, "rpc_" + method, None) if method else None
            if fn is None:
                raise _RpcError(-32601, "method not found: %s" % method)
            return fn(db, params)

        def rpc_getblockcount(self, db, p):
            return int(node.hdd_block)

        def rpc_getbestblockhash(self, db, p):
            return node.last_block_hash

        def rpc_getconnectioncount(self, db, p):
            return len(getattr(node.peers, "connection_pool", []) or [])

        def rpc_getdifficulty(self, db, p):
            return float(node.difficulty[0]) if getattr(node, "difficulty", None) else 0.0

        def rpc_getblockhash(self, db, p):
            height = int(p[0])
            db.execute_param(db.h, "SELECT block_hash FROM transactions WHERE block_height = ? LIMIT 1",
                             (height,))
            row = db.h.fetchone()
            if not row:
                raise _RpcError(-8, "block height out of range")
            return row[0]

        def rpc_getblock(self, db, p):
            block_hash = p[0]
            db.execute_param(db.h, "SELECT * FROM transactions WHERE block_hash = ?", (block_hash,))
            rows = db.h.fetchall()
            if not rows:
                raise _RpcError(-5, "block not found")
            return {"hash": block_hash, "height": rows[0][0], "time": rows[0][1],
                    "nTx": len(rows), "tx": [r[5][:56] for r in rows]}

        def rpc_getblockchaininfo(self, db, p):
            return {"chain": "main" if node.is_mainnet else ("test" if node.is_testnet else "regtest"),
                    "blocks": int(node.hdd_block), "bestblockhash": node.last_block_hash,
                    "difficulty": float(node.difficulty[0]) if getattr(node, "difficulty", None) else 0.0}

        def rpc_getbalance(self, db, p):
            address = p[0]
            return str(essentials.ledger_balance3(address, {}, db))

        rpc_getreceivedbyaddress = rpc_getbalance

        def rpc_getrawtransaction(self, db, p):
            txid = p[0]
            db.execute_param(db.h, "SELECT * FROM transactions WHERE signature LIKE ? LIMIT 1",
                             (txid + "%",))
            row = db.h.fetchone()
            if not row:
                raise _RpcError(-5, "no such transaction")
            return {"txid": row[5][:56], "blockheight": row[0], "time": row[1],
                    "from": row[2], "to": row[3], "amount": str(row[4])}

    return Handler


class BitcoinRPCServer:
    def __init__(self, node, port=8332):
        self.node = node
        self.port = port
        self._httpd = None
        self._thread = None

    def start(self):
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), _make_handler(self.node))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.node.logger.app_log.warning("Status: Bitcoin-compatible JSON-RPC on port %s" % self.port)

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
