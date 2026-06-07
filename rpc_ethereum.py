"""
Ethereum/ERC compatibility shim (doc/17) — exposes an ``eth_*`` JSON-RPC subset over Bismuth so
web3-style tooling can read chain data. Behind the ``rpc_ethereum`` flag (default off, port 8545).

HONESTLY BOUNDED — this is a *shim*, not an EVM and not drop-in MetaMask:
  * Bismuth addresses are 28 bytes (56 hex); Ethereum addresses are 20 bytes — they do not interconvert,
    so balance/tx calls take a Bismuth address, not an `0x`-eth address.
  * There is no gas/EVM/contract execution. `eth_call`/contract methods are out of scope.
What it does provide: chain height/blocks/tx reads and balances, hex-encoded the way `eth_*` clients
expect, for custom integrations and explorers.

Deps: stdlib only; reuses dbhandler + essentials (same as rpc_bitcoin.py).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dbhandler
import essentials

__version__ = "0.1.0"
_CHAIN_ID = 0xB15  # "BIS"


def _hex(n):
    return hex(int(n))


def _make_handler(node):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _db(self):
            return dbhandler.DbHandler(node.index_db, node.ledger_path, node.hyper_path, node.ram,
                                       node.ledger_ram_file, node.logger,
                                       trace_db_calls=node.trace_db_calls)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32700, "message": "parse error"}})
            rid = req.get("id")
            db = None
            try:
                db = self._db()
                self._send({"jsonrpc": "2.0", "id": rid,
                            "result": self._dispatch(db, req.get("method"), req.get("params") or [])})
            except Exception as e:
                self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})
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

        def _dispatch(self, db, method, params):
            fn = {"eth_blockNumber": self.blockNumber, "eth_chainId": self.chainId,
                  "net_version": self.netVersion, "web3_clientVersion": self.clientVersion,
                  "eth_gasPrice": self.gasPrice, "eth_getBalance": self.getBalance,
                  "eth_getBlockByNumber": self.getBlockByNumber,
                  "eth_getTransactionByHash": self.getTxByHash}.get(method)
            if fn is None:
                raise Exception("method not supported by shim: %s" % method)
            return fn(db, params)

        def blockNumber(self, db, p):
            return _hex(node.hdd_block)

        def chainId(self, db, p):
            return _hex(_CHAIN_ID)

        def netVersion(self, db, p):
            return str(_CHAIN_ID)

        def clientVersion(self, db, p):
            return "Bismuth/%s" % node.app_version

        def gasPrice(self, db, p):
            return "0x0"  # Bismuth uses fees, not gas

        def getBalance(self, db, p):
            # p[0] is a Bismuth address (not an 0x-eth address). Return wei-scaled (×1e18) so eth clients
            # render it as whole BIS.
            bal = essentials.ledger_balance3(p[0], {}, db)
            return _hex(int(float(bal) * (10 ** 18)))

        def getBlockByNumber(self, db, p):
            height = int(p[0], 16) if isinstance(p[0], str) and p[0].startswith("0x") else int(p[0])
            db.execute_param(db.h, "SELECT * FROM transactions WHERE block_height = ?", (height,))
            rows = db.h.fetchall()
            if not rows:
                return None
            return {"number": _hex(height), "hash": rows[0][7], "timestamp": _hex(int(float(rows[0][1]))),
                    "transactions": [r[5][:56] for r in rows]}

        def getTxByHash(self, db, p):
            db.execute_param(db.h, "SELECT * FROM transactions WHERE signature LIKE ? LIMIT 1",
                             (p[0] + "%",))
            row = db.h.fetchone()
            if not row:
                return None
            return {"hash": row[5][:56], "blockNumber": _hex(row[0]),
                    "from": row[2], "to": row[3], "value": _hex(int(float(row[4]) * (10 ** 18)))}

    return Handler


class EthereumRPCServer:
    def __init__(self, node, port=8545):
        self.node = node
        self.port = port
        self._httpd = None

    def start(self):
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), _make_handler(self.node))
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self.node.logger.app_log.warning("Status: Ethereum-compatible JSON-RPC shim on port %s" % self.port)

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
