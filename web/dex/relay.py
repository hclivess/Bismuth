#!/usr/bin/env python3
"""
relay.py — a tiny LOCAL signing/submit relay for the DEX demo UI (contracts/dex.py).

WHY THIS EXISTS  (identical rationale to the raffle / prediction-market relays)
  The node REST API is READ-ONLY and a browser can neither speak Bismuth's socket protocol nor sign a
  transaction. So the SPA reads order-book + balance state from the node REST API (GET /api/vm/contract/...)
  and POSTs the WRITE actions to THIS relay, which signs with a local wallet (in-tree polysign, like the
  test client) and submits over the socket protocol. It is NOT part of the node and changes NOTHING in
  consensus — a demo signer; bind it to localhost only.

AMOUNTS: token amounts are integer token-units; prices are entered in BIS and converted to atomic units
  (x1e8) for the contract (which compares them to callvalue, in units). A SELL escrows token (no value
  attached); a BUY attaches the price in BIS; filling a SELL attaches the order's price in BIS; filling a
  BUY attaches no value (the taker provides token). Price x 1e8 must fit 32 bits (price < ~42.9 BIS).

RUN
  python3 web/dex/relay.py --wallet wallet.der --node-port 3030 --listen 8099 [--api http://127.0.0.1:3031]

ENDPOINTS (JSON, CORS-enabled)
  GET  /relay/info                                          -> {address, admin_id, node_port, api, sink, unit}
  POST /relay/deploy   {"supply":<int>}                     -> {txid, supply}
  POST /relay/init     {"address"}                          -> {txid}                    (admin genesis mint)
  POST /relay/transfer {"address","to":"<56hex>","amt":N}   -> {txid}
  POST /relay/sell     {"address","token":N,"price":<bis>}  -> {txid}
  POST /relay/buy      {"address","token":N,"price":<bis>}  -> {txid}
  POST /relay/fill     {"address","oid":N,"side":1|2,"price":<bis>} -> {txid}
  POST /relay/cancel   {"address","oid":N}                  -> {txid}
  POST /relay/mine     {"count":N}                          -> {height, mined}
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
import dex                             # selectors + bytecode builder + key helpers
from _lite_client import LiteClient    # in-tree signer/submitter (polysign + socket protocol)

UNIT = 100_000_000


def _be4(x):
    return (int(x) & 0xFFFFFFFF).to_bytes(4, "big")


def _price_units(price_bis):
    u = int(round(float(price_bis) * UNIT))
    if not (0 < u <= 0xFFFFFFFF):
        raise ValueError("price must be > 0 and < ~42.9 BIS (price x 1e8 must fit a 32-bit word)")
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
        txid = self.client.send(recipient, float(bis), "vm:call", "%s:%s" % (addr, calldata_bytes.hex()))
        return txid

    def deploy(self, supply):
        supply = int(supply)
        if not (0 < supply <= 0xFFFFFFFF):
            raise ValueError("supply must be 1..2^32-1 token units")
        code_hex = dex.build(dex.party_id_of(self.address), supply).hex()
        txid = self.client.send(self.address, 1, "vm:deploy", code_hex)
        return {"txid": txid, "supply": supply, "code_size": len(code_hex) // 2}

    def init(self, addr):
        return {"txid": self._call(addr, _be4(dex.FN_INIT))}

    def transfer(self, addr, to, amt):
        to = (to or "").lower()
        if len(to) != 56 or any(c not in "0123456789abcdef" for c in to):
            raise ValueError("recipient must be a 56-hex Bismuth address")
        return {"txid": self._call(addr, _be4(dex.FN_TRANSFER) + bytes.fromhex(to) + _be4(amt))}

    def sell(self, addr, token, price):
        cd = _be4(dex.FN_SELL) + self._my28() + _be4(token) + _be4(_price_units(price))
        return {"txid": self._call(addr, cd)}                       # maker escrows token; no value attached

    def buy(self, addr, token, price):
        pu = _price_units(price)
        cd = _be4(dex.FN_BUY) + self._my28() + _be4(token) + _be4(pu)
        return {"txid": self._call(addr, cd, bis=pu / UNIT)}         # attach the BIS bid (escrowed)

    def fill(self, addr, oid, side, price=None):
        cd = _be4(dex.FN_FILL) + _be4(oid) + self._my28()
        bis = (_price_units(price) / UNIT) if int(side) == dex.SIDE_SELL else 0.0
        return {"txid": self._call(addr, cd, bis=bis)}              # SELL fill pays BIS; BUY fill pays token

    def cancel(self, addr, oid):
        return {"txid": self._call(addr, _be4(dex.FN_CANCEL) + _be4(oid))}

    def mine(self, count=1):
        self.client.mine(int(count))
        return {"height": self.client.block_height(), "mined": int(count)}

    def info(self, api):
        return {"address": self.address, "admin_id": dex.party_id_of(self.address),
                "node_port": self.node_port, "api": api, "sink": vm_engine.VM_SINK, "unit": UNIT}


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
                    return self._json(200, relay.deploy(body.get("supply", 1_000_000)))
                if path == "/relay/init":
                    return self._json(200, relay.init(body["address"]))
                if path == "/relay/transfer":
                    return self._json(200, relay.transfer(body["address"], body["to"], body["amt"]))
                if path == "/relay/sell":
                    return self._json(200, relay.sell(body["address"], body["token"], body["price"]))
                if path == "/relay/buy":
                    return self._json(200, relay.buy(body["address"], body["token"], body["price"]))
                if path == "/relay/fill":
                    return self._json(200, relay.fill(body["address"], body["oid"], body["side"],
                                                      body.get("price")))
                if path == "/relay/cancel":
                    return self._json(200, relay.cancel(body["address"], body["oid"]))
                if path == "/relay/mine":
                    return self._json(200, relay.mine(body.get("count", 1)))
                return self._json(404, {"error": "unknown endpoint"})
            except KeyError as e:
                return self._json(400, {"error": "missing field %s" % e})
            except Exception as e:
                return self._json(400, {"error": str(e)})

    return H


def main():
    ap = argparse.ArgumentParser(description="Local signing/submit relay for the DEX demo")
    ap.add_argument("--wallet", default=os.path.join(ROOT, "wallet.der"), help="wallet .der to sign with")
    ap.add_argument("--node-port", type=int, default=3030, help="node socket-protocol port (regnet=3030)")
    ap.add_argument("--listen", type=int, default=8099, help="port this relay listens on (localhost)")
    ap.add_argument("--api", default="http://127.0.0.1:3031", help="node REST API base the SPA should read")
    args = ap.parse_args()

    relay = Relay(args.wallet, args.node_port)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.listen), make_handler(relay, args.api))
    print("dex relay on http://127.0.0.1:%d" % args.listen)
    print("  wallet address : %s" % relay.address)
    print("  node port      : %d   REST api: %s" % (args.node_port, args.api))
    print("  open index.html and set Relay = http://127.0.0.1:%d , API = %s" % (args.listen, args.api))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
