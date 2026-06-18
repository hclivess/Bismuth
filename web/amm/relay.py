#!/usr/bin/env python3
"""
relay.py — a tiny LOCAL signing/submit relay for the AMM demo UI (contracts/amm.py).

WHY THIS EXISTS  (identical rationale to the dex / raffle / prediction-market relays)
  The node REST API is READ-ONLY and a browser can neither speak Bismuth's socket protocol nor sign a
  transaction. So the SPA reads pool state (reserves, LP shares, token balances) from the node REST API
  (GET /api/vm/contract/...) and POSTs the WRITE actions to THIS relay, which signs with a local wallet
  (in-tree polysign, like the test client) and submits over the socket protocol. It is NOT part of the
  node and changes NOTHING in consensus — a demo signer; bind it to localhost only.

AMOUNTS
  * BIS side  (dBis on add, dBis on a buy-swap, min-BIS-out on a sell-swap): entered in BIS. The amount
    ATTACHED to a value-bearing call (add / swap BIS->token) is sent in BIS and arrives at the contract as
    callvalue in atomic UNITS (x1e8). min_bis_out is converted to units for the calldata. Per the contract's
    MAX_RESERVE cap, each reserve tops out near 10.7 BIS (2^30 units).
  * TOKEN / SHARE side (tok_max, tok_in, min_tok_out, shares): entered directly in integer units.

RUN
  python3 web/amm/relay.py --wallet wallet.der --node-port 3030 --listen 8098 [--api http://127.0.0.1:3031]

ENDPOINTS (JSON, CORS-enabled)
  GET  /relay/info                                              -> {address, party_id, node_port, api, sink, unit, max_reserve, min_liq, fee_num, fee_den}
  POST /relay/deploy   {"supply":<int>}                         -> {txid, supply}
  POST /relay/init     {"address"}                              -> {txid}                 (admin genesis mint)
  POST /relay/transfer {"address","to":"<56hex>","amt":N}       -> {txid}
  POST /relay/add      {"address","bis":<bis>,"tok_max":N}      -> {txid}                 (attach `bis`, pull <=tok_max token)
  POST /relay/remove   {"address","shares":N}                   -> {txid}                 (burn shares -> BIS + token back)
  POST /relay/buy      {"address","bis":<bis>,"min_tok_out":N}  -> {txid}                 (swap BIS -> token)
  POST /relay/sell     {"address","tok_in":N,"min_bis":<bis>}   -> {txid}                 (swap token -> BIS)
  POST /relay/mine     {"count":N}                              -> {height, mined}
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
import amm                             # selectors + bytecode builder + key helpers
from _lite_client import LiteClient    # in-tree signer/submitter (polysign + socket protocol)

UNIT = 100_000_000


def _be4(x):
    return (int(x) & 0xFFFFFFFF).to_bytes(4, "big")


def _bis_to_units(bis):
    u = int(round(float(bis) * UNIT))
    if not (0 <= u <= 0xFFFFFFFF):
        raise ValueError("BIS amount x 1e8 must fit a 32-bit word (< ~42.9 BIS)")
    return u


class Relay:
    def __init__(self, wallet, node_port):
        self.client = LiteClient(wallet, port=int(node_port))
        self.node_port = int(node_port)

    @property
    def address(self):
        return self.client.address

    def _my28(self):
        return bytes.fromhex(self.address)        # the RSA wallet is a 56-hex == 28-byte address

    def _call(self, addr, calldata_bytes, bis=0.0):
        recipient = vm_engine.VM_SINK if bis else self.address
        return self.client.send(recipient, float(bis), "vm:call", "%s:%s" % (addr, calldata_bytes.hex()))

    def deploy(self, supply):
        supply = int(supply)
        if not (0 < supply <= 0xFFFFFFFF):
            raise ValueError("supply must be 1..2^32-1 token units")
        code_hex = amm.build(amm.party_id_of(self.address), supply).hex()
        txid = self.client.send(self.address, 1, "vm:deploy", code_hex)
        return {"txid": txid, "supply": supply, "code_size": len(code_hex) // 2}

    def init(self, addr):
        return {"txid": self._call(addr, _be4(amm.FN_INIT))}

    def transfer(self, addr, to, amt):
        to = (to or "").lower()
        if len(to) != 56 or any(c not in "0123456789abcdef" for c in to):
            raise ValueError("recipient must be a 56-hex Bismuth address")
        return {"txid": self._call(addr, _be4(amm.FN_TRANSFER) + bytes.fromhex(to) + _be4(amt))}

    def add(self, addr, bis, tok_max):
        cd = _be4(amm.FN_ADD_LIQ) + _be4(tok_max)
        return {"txid": self._call(addr, cd, bis=float(bis))}         # attach dBis; pull <= tok_max token

    def remove(self, addr, shares):
        cd = _be4(amm.FN_REMOVE_LIQ) + self._my28() + _be4(shares)
        return {"txid": self._call(addr, cd)}                         # burn shares; BIS + token come back

    def buy(self, addr, bis, min_tok_out=0):
        cd = _be4(amm.FN_SWAP_B2T) + _be4(min_tok_out)
        return {"txid": self._call(addr, cd, bis=float(bis))}         # swap BIS -> token

    def sell(self, addr, tok_in, min_bis=0.0):
        cd = _be4(amm.FN_SWAP_T2B) + self._my28() + _be4(tok_in) + _be4(_bis_to_units(min_bis))
        return {"txid": self._call(addr, cd)}                         # swap token -> BIS

    def mine(self, count=1):
        self.client.mine(int(count))
        return {"height": self.client.block_height(), "mined": int(count)}

    def info(self, api):
        return {"address": self.address, "party_id": amm.party_id_of(self.address),
                "node_port": self.node_port, "api": api, "sink": vm_engine.VM_SINK, "unit": UNIT,
                "max_reserve": amm.MAX_RESERVE, "min_liq": amm.MIN_LIQ,
                "fee_num": amm.FEE_NUM, "fee_den": amm.FEE_DEN,
                "slots": {"rbis": amm.S_RBIS, "rtok": amm.S_RTOK, "ltot": amm.S_LTOTAL,
                          "tag_bal": amm.TAG_BAL, "tag_lp": amm.TAG_LP, "user_mask": amm.USER_MASK}}


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
                    return self._json(200, relay.deploy(body.get("supply", 1_000_000_000)))
                if path == "/relay/init":
                    return self._json(200, relay.init(body["address"]))
                if path == "/relay/transfer":
                    return self._json(200, relay.transfer(body["address"], body["to"], body["amt"]))
                if path == "/relay/add":
                    return self._json(200, relay.add(body["address"], body["bis"], body["tok_max"]))
                if path == "/relay/remove":
                    return self._json(200, relay.remove(body["address"], body["shares"]))
                if path == "/relay/buy":
                    return self._json(200, relay.buy(body["address"], body["bis"], body.get("min_tok_out", 0)))
                if path == "/relay/sell":
                    return self._json(200, relay.sell(body["address"], body["tok_in"], body.get("min_bis", 0.0)))
                if path == "/relay/mine":
                    return self._json(200, relay.mine(body.get("count", 1)))
                return self._json(404, {"error": "unknown endpoint"})
            except KeyError as e:
                return self._json(400, {"error": "missing field %s" % e})
            except Exception as e:
                return self._json(400, {"error": str(e)})

    return H


def main():
    ap = argparse.ArgumentParser(description="Local signing/submit relay for the AMM demo")
    ap.add_argument("--wallet", default=os.path.join(ROOT, "wallet.der"), help="wallet .der to sign with")
    ap.add_argument("--node-port", type=int, default=3030, help="node socket-protocol port (regnet=3030)")
    ap.add_argument("--listen", type=int, default=8098, help="port this relay listens on (localhost)")
    ap.add_argument("--api", default="http://127.0.0.1:3031", help="node REST API base the SPA should read")
    args = ap.parse_args()

    relay = Relay(args.wallet, args.node_port)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.listen), make_handler(relay, args.api))
    print("amm relay on http://127.0.0.1:%d" % args.listen)
    print("  wallet address : %s" % relay.address)
    print("  node port      : %d   REST api: %s" % (args.node_port, args.api))
    print("  open index.html and set Relay = http://127.0.0.1:%d , API = %s" % (args.listen, args.api))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
