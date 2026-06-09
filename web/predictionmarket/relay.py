#!/usr/bin/env python3
"""
relay.py — a tiny LOCAL signing/submit relay for the prediction-market demo UI.

WHY THIS EXISTS
  The node's REST API (rest_api.py) is deliberately READ-ONLY (GET) so it can never affect consensus —
  it has no transaction-submission endpoint. A browser, meanwhile, cannot speak Bismuth's length-prefixed
  socket protocol nor sign an RSA transaction. So the demo SPA reads chain/market state directly from the
  node REST API (GET /api/...), and for the four WRITE actions (deploy / buy / resolve / claim) it POSTs
  to THIS relay, which signs with a local wallet (the in-tree polysign, exactly like the test client) and
  submits over the existing socket protocol.

  It is NOT part of the node and changes NOTHING in consensus: it is a thin convenience wrapper around the
  same LiteClient the test-suite already uses (essentials.sign_rsa + connections.send). Run it locally,
  next to a node you control, with the wallet whose key should sign. Treat it as a demo signer, not a
  production service — it signs whatever the page asks, so bind it to localhost only.

RUN
  python3 web/predictionmarket/relay.py \
      --wallet wallet.der --node-port 3030 --listen 8099 [--api http://127.0.0.1:3031]

ENDPOINTS (all JSON, CORS-enabled for the SPA)
  GET  /relay/info                      -> {address, resolver_id, node_port, api}
  POST /relay/deploy {"resolver":"<56hex addr>"|null}      -> deploy a fresh market, returns {txid}
  POST /relay/buy    {"address","side":"yes"|"no","bis":<float>}   -> buy shares, returns {txid}
  POST /relay/resolve{"address","outcome":1|2}             -> settle (resolver only), returns {txid}
  POST /relay/claim  {"address","recipient":"<56hex>"}     -> claim winnings, returns {txid}

  The relay does NOT mine. On regnet, mine with the node's regtest_generate (the UI exposes a "mine"
  button that calls POST /relay/mine on regnet for convenience); on a real chain, miners confirm txs.
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tests"))
if os.path.join(ROOT, "contracts") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "contracts"))

import vm_engine                       # VM_SINK custody address
import prediction_market as pm         # selectors + bytecode builder
from _lite_client import LiteClient    # in-tree signer/submitter (polysign + socket protocol)

UNIT = 100_000_000                     # 1 BIS in atomic units (ledger_integer_amounts)


class Relay:
    def __init__(self, wallet, node_port):
        self.client = LiteClient(wallet, port=int(node_port))
        self.node_port = int(node_port)

    @property
    def address(self):
        return self.client.address

    def _sel(self, n):
        return n.to_bytes(4, "big").hex()

    def deploy(self, resolver_addr):
        """Deploy a fresh binary market. resolver_addr (56-hex) defaults to this relay's wallet."""
        resolver = resolver_addr or self.address
        rid = pm.resolver_id_of(resolver)
        code_hex = pm.build(rid).hex()
        txid = self.client.send(self.address, 1, "vm:deploy", code_hex)
        return {"txid": txid, "resolver": resolver, "resolver_id": rid, "code_size": len(code_hex) // 2}

    def buy(self, addr, side, bis):
        side = str(side).lower()
        if side not in ("yes", "no"):
            raise ValueError("side must be 'yes' or 'no'")
        if float(bis) <= 0:
            raise ValueError("bis must be > 0")
        sel = pm.FN_BUY_YES if side == "yes" else pm.FN_BUY_NO
        # A deposit rides to the VM custody sink; the calldata is just the selector.
        txid = self.client.send(vm_engine.VM_SINK, float(bis), "vm:call",
                                "%s:%s" % (addr, self._sel(sel)))
        return {"txid": txid, "side": side, "bis": float(bis)}

    def resolve(self, addr, outcome):
        outcome = int(outcome)
        if outcome not in (1, 2):
            raise ValueError("outcome must be 1 (YES) or 2 (NO)")
        calldata = self._sel(pm.FN_RESOLVE) + outcome.to_bytes(4, "big").hex()
        txid = self.client.send(self.address, 0, "vm:call", "%s:%s" % (addr, calldata))
        return {"txid": txid, "outcome": outcome}

    def claim(self, addr, recipient):
        recipient = (recipient or self.address).lower()
        if len(recipient) != 56 or any(c not in "0123456789abcdef" for c in recipient):
            raise ValueError("recipient must be a 56-hex Bismuth address")
        calldata = self._sel(pm.FN_CLAIM) + recipient        # recipient is exactly 28 bytes = 56 hex
        txid = self.client.send(self.address, 0, "vm:call", "%s:%s" % (addr, calldata))
        return {"txid": txid, "recipient": recipient}

    def mine(self, count=1):
        """REGNET convenience only: instantly generate `count` blocks so the demo confirms without a miner.
        Harmless on regnet; a no-op-ish error on mainnet (the command won't be accepted)."""
        self.client.mine(int(count))
        return {"height": self.client.block_height(), "mined": int(count)}

    def info(self, api):
        return {"address": self.address, "resolver_id": pm.resolver_id_of(self.address),
                "node_port": self.node_port, "api": api, "sink": vm_engine.VM_SINK}


def make_handler(relay, api):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _json(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if self.path.rstrip("/") == "/relay/info":
                return self._json(200, relay.info(api))
            return self._json(404, {"error": "unknown endpoint"})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                return self._json(400, {"error": "bad json: %s" % e})
            path = self.path.rstrip("/")
            try:
                if path == "/relay/deploy":
                    return self._json(200, relay.deploy(body.get("resolver")))
                if path == "/relay/buy":
                    return self._json(200, relay.buy(body["address"], body["side"], body["bis"]))
                if path == "/relay/resolve":
                    return self._json(200, relay.resolve(body["address"], body["outcome"]))
                if path == "/relay/claim":
                    return self._json(200, relay.claim(body["address"], body.get("recipient")))
                if path == "/relay/mine":
                    return self._json(200, relay.mine(body.get("count", 1)))
                return self._json(404, {"error": "unknown endpoint"})
            except KeyError as e:
                return self._json(400, {"error": "missing field %s" % e})
            except Exception as e:
                return self._json(400, {"error": str(e)})

    return H


def main():
    ap = argparse.ArgumentParser(description="Local signing/submit relay for the prediction-market demo")
    ap.add_argument("--wallet", default=os.path.join(ROOT, "wallet.der"), help="wallet .der to sign with")
    ap.add_argument("--node-port", type=int, default=3030, help="node socket-protocol port (regnet=3030)")
    ap.add_argument("--listen", type=int, default=8099, help="port this relay listens on (localhost)")
    ap.add_argument("--api", default="http://127.0.0.1:3031", help="node REST API base the SPA should read")
    args = ap.parse_args()

    relay = Relay(args.wallet, args.node_port)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.listen), make_handler(relay, args.api))
    print("prediction-market relay on http://127.0.0.1:%d" % args.listen)
    print("  wallet address : %s" % relay.address)
    print("  resolver_id    : %d" % pm.resolver_id_of(relay.address))
    print("  node port      : %d   REST api: %s" % (args.node_port, args.api))
    print("  open the UI and set Relay = http://127.0.0.1:%d , API = %s" % (args.listen, args.api))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
