#!/usr/bin/env python3
"""
relay.py — a tiny LOCAL signing/submit relay for the raffle demo UI (contracts/raffle.py).

WHY THIS EXISTS  (identical rationale to the prediction-market relay)
  The node REST API is deliberately READ-ONLY, and a browser can neither speak Bismuth's socket protocol
  nor sign a transaction. So the SPA reads contract state from the node REST API (GET /api/vm/contract/...)
  and POSTs the four WRITE actions (deploy / enter / draw / claim) to THIS relay, which signs with a local
  wallet (in-tree polysign, exactly like the test client) and submits over the socket protocol. It is NOT
  part of the node and changes NOTHING in consensus — a demo signer; bind it to localhost only.

THE SECRET SEED (commit-reveal)
  At DEPLOY the relay generates a random 32-byte seed, bakes only its SHA256 into the contract, and
  RETURNS the seed to the caller. The admin keeps it secret until the draw, then reveals it via /relay/draw
  (the relay also remembers the last-deployed seed as a convenience). The contract verifies the reveal.

RUN
  python3 web/raffle/relay.py --wallet wallet.der --node-port 3030 --listen 8098 [--api http://127.0.0.1:3031]

ENDPOINTS (JSON, CORS-enabled)
  GET  /relay/info                                  -> {address, admin_id, node_port, api, sink}
  POST /relay/deploy {}                             -> {txid, seed, seed_hash}   (SAVE the seed!)
  POST /relay/enter  {"address","bis":<float>}      -> {txid, bis}
  POST /relay/draw   {"address","seed":"<64hex>"?}  -> {txid, seed}   (seed optional: defaults to last)
  POST /relay/claim  {"address","recipient":"<56hex>"} -> {txid, recipient}
  POST /relay/mine   {"count":N}                    -> {height, mined}   (regnet convenience)
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (ROOT, os.path.join(ROOT, "tests"), os.path.join(ROOT, "contracts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import vm_engine                       # VM_SINK custody address
import raffle                          # selectors + bytecode builder + commitment helpers
from _lite_client import LiteClient    # in-tree signer/submitter (polysign + socket protocol)


class Relay:
    def __init__(self, wallet, node_port):
        self.client = LiteClient(wallet, port=int(node_port))
        self.node_port = int(node_port)
        self._last_seed = None          # remembered across a deploy so /relay/draw can default to it

    @property
    def address(self):
        return self.client.address

    def _sel(self, n):
        return n.to_bytes(4, "big").hex()

    def deploy(self):
        """Deploy a fresh raffle with this wallet as admin and a freshly generated committed seed."""
        seed = os.urandom(32)
        self._last_seed = seed.hex()
        code_hex = raffle.build(raffle.party_id_of(self.address), raffle.seed_hash_of(seed)).hex()
        txid = self.client.send(self.address, 1, "vm:deploy", code_hex)
        # NOTE: the seed is returned so the admin can keep it and reveal it at draw. Keep it secret.
        return {"txid": txid, "seed": seed.hex(), "seed_hash": raffle.seed_hash_of(seed).hex(),
                "code_size": len(code_hex) // 2}

    def enter(self, addr, bis):
        if float(bis) <= 0:
            raise ValueError("bis must be > 0")
        txid = self.client.send(vm_engine.VM_SINK, float(bis), "vm:call",
                                "%s:%s" % (addr, self._sel(raffle.FN_ENTER)))
        return {"txid": txid, "bis": float(bis)}

    def draw(self, addr, seed=None):
        seed_hex = (seed or self._last_seed or "").lower()
        if len(seed_hex) != 64 or any(c not in "0123456789abcdef" for c in seed_hex):
            raise ValueError("seed must be 64 hex chars (the 32-byte secret revealed); none remembered")
        calldata = self._sel(raffle.FN_DRAW) + seed_hex
        txid = self.client.send(self.address, 0, "vm:call", "%s:%s" % (addr, calldata))
        return {"txid": txid, "seed": seed_hex}

    def claim(self, addr, recipient):
        recipient = (recipient or self.address).lower()
        if len(recipient) != 56 or any(c not in "0123456789abcdef" for c in recipient):
            raise ValueError("recipient must be a 56-hex Bismuth address")
        calldata = self._sel(raffle.FN_CLAIM) + recipient     # recipient is exactly 28 bytes = 56 hex
        txid = self.client.send(self.address, 0, "vm:call", "%s:%s" % (addr, calldata))
        return {"txid": txid, "recipient": recipient}

    def mine(self, count=1):
        """REGNET convenience only: instantly generate `count` blocks so the demo confirms without a miner."""
        self.client.mine(int(count))
        return {"height": self.client.block_height(), "mined": int(count)}

    def info(self, api):
        return {"address": self.address, "admin_id": raffle.party_id_of(self.address),
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
            self.send_response(204); self._cors(); self.end_headers()

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
                    return self._json(200, relay.deploy())
                if path == "/relay/enter":
                    return self._json(200, relay.enter(body["address"], body["bis"]))
                if path == "/relay/draw":
                    return self._json(200, relay.draw(body["address"], body.get("seed")))
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
    ap = argparse.ArgumentParser(description="Local signing/submit relay for the raffle demo")
    ap.add_argument("--wallet", default=os.path.join(ROOT, "wallet.der"), help="wallet .der to sign with")
    ap.add_argument("--node-port", type=int, default=3030, help="node socket-protocol port (regnet=3030)")
    ap.add_argument("--listen", type=int, default=8098, help="port this relay listens on (localhost)")
    ap.add_argument("--api", default="http://127.0.0.1:3031", help="node REST API base the SPA should read")
    args = ap.parse_args()

    relay = Relay(args.wallet, args.node_port)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.listen), make_handler(relay, args.api))
    print("raffle relay on http://127.0.0.1:%d" % args.listen)
    print("  wallet address : %s" % relay.address)
    print("  node port      : %d   REST api: %s" % (args.node_port, args.api))
    print("  open index.html and set Relay = http://127.0.0.1:%d , API = %s" % (args.listen, args.api))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
