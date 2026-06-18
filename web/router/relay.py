#!/usr/bin/env python3
"""
relay.py — a tiny LOCAL signing/submit relay for the multi-pool AMM router demo UI (contracts/router.py).

WHY THIS EXISTS  (identical rationale to the amm / dex relays)
  The node REST API is READ-ONLY and a browser can neither speak Bismuth's socket protocol nor sign a
  transaction. So the SPA reads pool/balance state from the node REST API (GET /api/vm/contract/...) and
  POSTs the WRITE actions to THIS relay, which signs with a local wallet and submits over the socket
  protocol. NOT part of the node; changes NOTHING in consensus — a demo signer; bind to localhost only.

AMOUNTS
  * BIS side (dBis on add, dBis on a buy-swap, min-BIS-out on a sell-swap): entered in BIS; the amount
    ATTACHED to a value-bearing call arrives as callvalue in atomic UNITS (x1e8). Each reserve caps at
    MAX_RESERVE (~10.7 BIS). min_bis_out is converted to units for the calldata.
  * TOKEN / SHARE side (tid, tok_max, tok_in, shares, amt_in, min_out): integer units.

RUN
  python3 web/router/relay.py --wallet wallet.der --node-port 3030 --listen 8097 [--api http://127.0.0.1:3031]

ENDPOINTS (JSON, CORS-enabled)
  GET  /relay/info                                                  -> {address, party_id, ...constants/slots}
  POST /relay/create   {"supply":N}                                 -> {txid}        (admin mints a new token tid)
  POST /relay/transfer {"address","tid":N,"to":"<56hex>","amt":N}   -> {txid}
  POST /relay/add      {"address","tid":N,"bis":<bis>,"tok_max":N}  -> {txid}
  POST /relay/remove   {"address","tid":N,"shares":N}               -> {txid}
  POST /relay/buy      {"address","tid":N,"bis":<bis>,"min_tok_out":N}        -> {txid}   (BIS -> token tid)
  POST /relay/sell     {"address","tid":N,"tok_in":N,"min_bis":<bis>}        -> {txid}   (token tid -> BIS)
  POST /relay/route    {"address","tid_in":N,"tid_out":N,"amt_in":N,"min_out":N} -> {txid}  (token -> token)
  POST /relay/mine     {"count":N}                                  -> {height, mined}
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

import vm_engine
import router
from _lite_client import LiteClient

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
        return bytes.fromhex(self.address)

    def _call(self, addr, calldata_bytes, bis=0.0):
        recipient = vm_engine.VM_SINK if bis else self.address
        return self.client.send(recipient, float(bis), "vm:call", "%s:%s" % (addr, calldata_bytes.hex()))

    def deploy(self):
        code_hex = router.build(router.party_id_of(self.address)).hex()
        txid = self.client.send(self.address, 1, "vm:deploy", code_hex)
        return {"txid": txid, "code_size": len(code_hex) // 2}

    def create(self, addr, supply):
        supply = int(supply)
        if not (0 < supply <= 0xFFFFFFFF):
            raise ValueError("supply must be 1..2^32-1 token units")
        return {"txid": self._call(addr, _be4(router.FN_CREATE) + _be4(supply))}

    def transfer(self, addr, tid, to, amt):
        to = (to or "").lower()
        if len(to) != 56 or any(c not in "0123456789abcdef" for c in to):
            raise ValueError("recipient must be a 56-hex Bismuth address")
        cd = _be4(router.FN_TRANSFER) + _be4(tid) + bytes.fromhex(to) + _be4(amt)
        return {"txid": self._call(addr, cd)}

    def add(self, addr, tid, bis, tok_max):
        cd = _be4(router.FN_ADD_LIQ) + _be4(tid) + _be4(tok_max)
        return {"txid": self._call(addr, cd, bis=float(bis))}

    def remove(self, addr, tid, shares):
        cd = _be4(router.FN_REMOVE_LIQ) + _be4(tid) + self._my28() + _be4(shares)
        return {"txid": self._call(addr, cd)}

    def buy(self, addr, tid, bis, min_tok_out=0):
        cd = _be4(router.FN_SWAP_B2T) + _be4(tid) + _be4(min_tok_out)
        return {"txid": self._call(addr, cd, bis=float(bis))}

    def sell(self, addr, tid, tok_in, min_bis=0.0):
        cd = _be4(router.FN_SWAP_T2B) + _be4(tid) + self._my28() + _be4(tok_in) + _be4(_bis_to_units(min_bis))
        return {"txid": self._call(addr, cd)}

    def route(self, addr, tid_in, tid_out, amt_in, min_out=0):
        cd = _be4(router.FN_SWAP_T2T) + _be4(tid_in) + _be4(tid_out) + _be4(amt_in) + _be4(min_out)
        return {"txid": self._call(addr, cd)}

    def mine(self, count=1):
        self.client.mine(int(count))
        return {"height": self.client.block_height(), "mined": int(count)}

    def info(self, api):
        return {"address": self.address, "party_id": router.party_id_of(self.address),
                "node_port": self.node_port, "api": api, "sink": vm_engine.VM_SINK, "unit": UNIT,
                "max_reserve": router.MAX_RESERVE, "min_liq": router.MIN_LIQ, "max_tokens": router.MAX_TOKENS,
                "fee_num": router.FEE_NUM, "fee_den": router.FEE_DEN,
                "slots": {"next_tid": router.S_NEXT_TID, "tag_pool": router.TAG_POOL, "tag_bal": router.TAG_BAL,
                          "tag_lp": router.TAG_LP, "user_mask": router.USER_MASK,
                          "rbis": router.POOL_RBIS, "rtok": router.POOL_RTOK, "ltot": router.POOL_L}}


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
                if path == "/relay/create":
                    return self._json(200, relay.create(body["address"], body.get("supply", 1_000_000_000)))
                if path == "/relay/transfer":
                    return self._json(200, relay.transfer(body["address"], body["tid"], body["to"], body["amt"]))
                if path == "/relay/add":
                    return self._json(200, relay.add(body["address"], body["tid"], body["bis"], body["tok_max"]))
                if path == "/relay/remove":
                    return self._json(200, relay.remove(body["address"], body["tid"], body["shares"]))
                if path == "/relay/buy":
                    return self._json(200, relay.buy(body["address"], body["tid"], body["bis"],
                                                     body.get("min_tok_out", 0)))
                if path == "/relay/sell":
                    return self._json(200, relay.sell(body["address"], body["tid"], body["tok_in"],
                                                      body.get("min_bis", 0.0)))
                if path == "/relay/route":
                    return self._json(200, relay.route(body["address"], body["tid_in"], body["tid_out"],
                                                       body["amt_in"], body.get("min_out", 0)))
                if path == "/relay/mine":
                    return self._json(200, relay.mine(body.get("count", 1)))
                return self._json(404, {"error": "unknown endpoint"})
            except KeyError as e:
                return self._json(400, {"error": "missing field %s" % e})
            except Exception as e:
                return self._json(400, {"error": str(e)})

    return H


def main():
    ap = argparse.ArgumentParser(description="Local signing/submit relay for the multi-pool router demo")
    ap.add_argument("--wallet", default=os.path.join(ROOT, "wallet.der"))
    ap.add_argument("--node-port", type=int, default=3030)
    ap.add_argument("--listen", type=int, default=8097)
    ap.add_argument("--api", default="http://127.0.0.1:3031")
    args = ap.parse_args()

    relay = Relay(args.wallet, args.node_port)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.listen), make_handler(relay, args.api))
    print("router relay on http://127.0.0.1:%d" % args.listen)
    print("  wallet address : %s" % relay.address)
    print("  node port      : %d   REST api: %s" % (args.node_port, args.api))
    print("  open index.html and set Relay = http://127.0.0.1:%d , API = %s" % (args.listen, args.api))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
