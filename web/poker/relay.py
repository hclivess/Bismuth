#!/usr/bin/env python3
"""
relay.py — a LOCAL signing/submit relay + tiny message bus for the heads-up poker demo (contracts/poker.py).

WHY THIS EXISTS
  Same as the other demo relays: the node REST API is read-only and a browser cannot sign or speak the
  socket protocol, so the SPA reads table state from the node REST API and POSTs the on-chain WRITE actions
  (stake/join/commit/play/fold/board/reveal/timeout) to THIS relay, which signs with a local wallet and
  submits. NOT part of the node; bind to localhost only.

PLUS A MESSAGE BUS  (/relay/msg, /relay/inbox)
  The mental-poker DEAL (the cooperative commutative-cipher shuffle that keeps hole cards private) runs
  peer-to-peer between the two players' browsers. This relay carries those off-chain messages — it only
  PASSES them along (in-memory, per table); the secret keys never leave the browsers, so the relay learns
  nothing about anyone's cards. (Each player runs their own local relay+wallet; the message bus lets the two
  clients find each other. For a single-machine demo, two browser tabs share one relay.)

RUN
  python3 web/poker/relay.py --wallet wallet.der --node-port 3030 --listen 8096 [--api http://127.0.0.1:3031]

ENDPOINTS (JSON, CORS-enabled)
  GET  /relay/info                                          -> {address, party_id, unit, ...constants}
  POST /relay/deploy   {"buyin":<bis>}                      -> {txid}     (deploy a fresh table; you are P0)
  POST /relay/stake0   {"address"}                          -> {txid}     (P0 stakes the buyin)
  POST /relay/join     {"address"}                          -> {txid}     (P1 stakes the buyin)
  POST /relay/commit   {"address","commit":"<64hex>"}       -> {txid}
  POST /relay/play     {"address"}                          -> {txid}
  POST /relay/fold     {"address"}                          -> {txid}
  POST /relay/board    {"address","cards":[c0..c4]}         -> {txid}
  POST /relay/reveal   {"address","hole":[h0,h1],"nonce":"<64hex>","idx":[i0..i4]} -> {txid}
  POST /relay/timeout  {"address"}                          -> {txid}
  POST /relay/mine     {"count":N}                          -> {height}
  POST /relay/msg      {"table","msg":{...}}                -> {ok}       (push a deal message for `table`)
  GET  /relay/inbox?table=<addr>&since=<n>                  -> {msgs:[...], next:<n>}
"""
import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (ROOT, os.path.join(ROOT, "tests"), os.path.join(ROOT, "contracts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import vm_engine
import poker
from _lite_client import LiteClient

UNIT = 100_000_000


def _be4(x):
    return (int(x) & 0xFFFFFFFF).to_bytes(4, "big")


def _bis_to_units(bis):
    u = int(round(float(bis) * UNIT))
    if not (0 < u <= 0xFFFFFFFF):
        raise ValueError("buyin x 1e8 must fit a 32-bit word")
    return u


class Relay:
    def __init__(self, wallet, node_port):
        self.client = LiteClient(wallet, port=int(node_port))
        self.node_port = int(node_port)
        self._bus = {}            # table -> list of messages
        self._lock = threading.Lock()

    @property
    def address(self):
        return self.client.address

    def _my28(self):
        return bytes.fromhex(self.address)

    def _call(self, addr, calldata_bytes, bis=0.0):
        recipient = vm_engine.VM_SINK if bis else self.address
        return self.client.send(recipient, float(bis), "vm:call", "%s:%s" % (addr, calldata_bytes.hex()))

    def deploy(self, buyin_bis):
        buyin = _bis_to_units(buyin_bis)
        code_hex = poker.build(poker.party_id_of(self.address), buyin).hex()
        return {"txid": self.client.send(self.address, 1, "vm:deploy", code_hex), "buyin_units": buyin}

    def stake0(self, addr, buyin_bis):
        return {"txid": self._call(addr, _be4(poker.FN_STAKE0) + self._my28(), bis=float(buyin_bis))}

    def join(self, addr, buyin_bis):
        return {"txid": self._call(addr, _be4(poker.FN_JOIN) + self._my28(), bis=float(buyin_bis))}

    def commit(self, addr, commit_hex):
        c = bytes.fromhex(commit_hex)
        if len(c) != 32:
            raise ValueError("commit must be 32 bytes (64 hex)")
        return {"txid": self._call(addr, _be4(poker.FN_COMMIT) + c)}

    def play(self, addr):
        return {"txid": self._call(addr, _be4(poker.FN_PLAY))}

    def fold(self, addr):
        return {"txid": self._call(addr, _be4(poker.FN_FOLD))}

    def board(self, addr, cards):
        cards = [int(c) for c in cards]
        if len(cards) != 5 or any(not (0 <= c <= 51) for c in cards):
            raise ValueError("board must be 5 cards in 0..51")
        return {"txid": self._call(addr, _be4(poker.FN_BOARD) + bytes(cards))}

    def reveal(self, addr, hole, nonce_hex, idx):
        hole = [int(hole[0]), int(hole[1])]
        nonce = bytes.fromhex(nonce_hex)
        idx = [int(i) for i in idx]
        if len(nonce) != 32 or len(idx) != 5:
            raise ValueError("nonce 32 bytes, idx 5 entries")
        cd = _be4(poker.FN_REVEAL) + bytes(hole) + nonce + bytes(idx)
        return {"txid": self._call(addr, cd)}

    def timeout(self, addr):
        return {"txid": self._call(addr, _be4(poker.FN_TIMEOUT))}

    def mine(self, count=1):
        self.client.mine(int(count))
        return {"height": self.client.block_height(), "mined": int(count)}

    def push_msg(self, table, msg):
        with self._lock:
            self._bus.setdefault(table, []).append(msg)
        return {"ok": True}

    def inbox(self, table, since):
        with self._lock:
            msgs = self._bus.get(table, [])
            return {"msgs": msgs[since:], "next": len(msgs)}

    def info(self, api):
        return {"address": self.address, "party_id": poker.party_id_of(self.address),
                "node_port": self.node_port, "api": api, "sink": vm_engine.VM_SINK, "unit": UNIT,
                "phases": {"OPEN": poker.PH_OPEN, "WAIT_P1": poker.PH_WAIT_P1, "COMMIT": poker.PH_COMMIT,
                           "ACT_P0": poker.PH_ACT_P0, "ACT_P1": poker.PH_ACT_P1, "BOARD": poker.PH_BOARD,
                           "SHOWDOWN": poker.PH_SHOWDOWN, "DONE": poker.PH_DONE},
                "slots": {"phase": poker.S_PHASE, "reveal0": poker.S_REVEAL0, "reveal1": poker.S_REVEAL1,
                          "rank0": poker.S_RANK0, "rank1": poker.S_RANK1},
                "tags": {"p0addr": poker.TAG_P0ADDR, "p1addr": poker.TAG_P1ADDR,
                         "commit0": poker.TAG_COMMIT0, "commit1": poker.TAG_COMMIT1, "board": poker.TAG_BOARD}}


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
            self.send_response(code); self.send_header("Content-Type", "application/json"); self._cors()
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204); self._cors(); self.end_headers()

        def do_GET(self):
            u = urlparse(self.path)
            if u.path.rstrip("/") == "/relay/info":
                return self._json(200, relay.info(api))
            if u.path.rstrip("/") == "/relay/inbox":
                q = parse_qs(u.query)
                return self._json(200, relay.inbox(q.get("table", [""])[0], int(q.get("since", ["0"])[0])))
            return self._json(404, {"error": "unknown endpoint"})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                b = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                return self._json(400, {"error": "bad json: %s" % e})
            path = self.path.rstrip("/")
            try:
                if path == "/relay/deploy":   return self._json(200, relay.deploy(b.get("buyin", 1.0)))
                if path == "/relay/stake0":   return self._json(200, relay.stake0(b["address"], b.get("buyin", 1.0)))
                if path == "/relay/join":     return self._json(200, relay.join(b["address"], b.get("buyin", 1.0)))
                if path == "/relay/commit":   return self._json(200, relay.commit(b["address"], b["commit"]))
                if path == "/relay/play":     return self._json(200, relay.play(b["address"]))
                if path == "/relay/fold":     return self._json(200, relay.fold(b["address"]))
                if path == "/relay/board":    return self._json(200, relay.board(b["address"], b["cards"]))
                if path == "/relay/reveal":   return self._json(200, relay.reveal(b["address"], b["hole"], b["nonce"], b["idx"]))
                if path == "/relay/timeout":  return self._json(200, relay.timeout(b["address"]))
                if path == "/relay/mine":     return self._json(200, relay.mine(b.get("count", 1)))
                if path == "/relay/msg":      return self._json(200, relay.push_msg(b["table"], b["msg"]))
                return self._json(404, {"error": "unknown endpoint"})
            except KeyError as e:
                return self._json(400, {"error": "missing field %s" % e})
            except Exception as e:
                return self._json(400, {"error": str(e)})

    return H


def main():
    ap = argparse.ArgumentParser(description="Local signing/submit relay + deal message bus for the poker demo")
    ap.add_argument("--wallet", default=os.path.join(ROOT, "wallet.der"))
    ap.add_argument("--node-port", type=int, default=3030)
    ap.add_argument("--listen", type=int, default=8096)
    ap.add_argument("--api", default="http://127.0.0.1:3031")
    args = ap.parse_args()
    relay = Relay(args.wallet, args.node_port)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.listen), make_handler(relay, args.api))
    print("poker relay on http://127.0.0.1:%d" % args.listen)
    print("  wallet address : %s" % relay.address)
    print("  node port      : %d   REST api: %s" % (args.node_port, args.api))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
