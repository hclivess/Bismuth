#!/usr/bin/env python3
"""
relay.py — the Stage-3 poker relay, trimmed to its TWO honest jobs (doc/33 §7):

  (a) MESSAGE BUS  (/relay/msg, /relay/inbox)  — DEFAULT, always on.
      The N-party mental-poker DEAL (the cooperative commutative-cipher shuffle that keeps hole cards
      private) runs peer-to-peer between the players' browsers. This relay only PASSES those off-chain
      messages along (in-memory, per table); it is KEY-BLIND — secret keys never leave the browsers,
      victim holeparts are e2e-encrypted to the victim, so the relay learns nothing about anyone's cards
      and cannot move funds. For a single-machine demo, several browser tabs share one relay.

  (b) DEV-ONLY FALLBACK SIGNER  (--dev-sign wallet.der)  — OPT-IN, off by default.
      A CUSTODIAL signer for headless/regnet CI and power-user self-host: it holds a wallet.der and
      signs+submits vm:calls on the browser's behalf, exactly like the pre-Stage-3 demo crutch. This is
      NOT how players should play — the browser wallet (web/wallet/) signs client-side and the keys never
      leave the browser. The flag prints a LOUD banner; default (no flag) exposes bus endpoints only.

The SPA (web/poker/index.html) now signs every on-chain WRITE through window.bismuth (the browser wallet),
reads table state from the node REST API directly, and uses THIS relay only as the deal message bus.

RUN
  python3 web/poker/relay.py                                   # bus only (production-shaped)
  python3 web/poker/relay.py --dev-sign wallet.der --node-port 3030   # + custodial dev signer (regnet)

ENDPOINTS (JSON, CORS-enabled)
  POST /relay/msg     {"table","msg":{...}}      -> {ok}        (push a deal message for `table`)
  GET  /relay/inbox?table=<addr>&since=<n>       -> {msgs:[...], next:<n>}
  GET  /relay/info                               -> {bus:true, dev_sign:<bool>, address?, ...}
  --- the following exist ONLY with --dev-sign (else 404) ---
  POST /relay/deploy  {"nseats","sb","bb","max_buyin"}   -> {txid,address?}
  POST /relay/vmcall  {"address","calldata_hex","value"} -> {txid}   (sign+submit any vm:call)
  POST /relay/mine    {"count":N}                        -> {height}
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

UNIT = 100_000_000


class Bus:
    """The key-blind message bus. Holds nothing but opaque per-table message lists in memory."""

    def __init__(self):
        self._bus = {}            # table -> list of messages
        self._lock = threading.Lock()

    def push_msg(self, table, msg):
        if not table:
            raise ValueError("missing table")
        with self._lock:
            self._bus.setdefault(table, []).append(msg)
        return {"ok": True}

    def inbox(self, table, since):
        with self._lock:
            msgs = self._bus.get(table, [])
            return {"msgs": msgs[since:], "next": len(msgs)}


class DevSigner:
    """OPT-IN custodial signer for regnet/CI smoke only. Wraps the in-tree LiteClient + poker_table build()."""

    def __init__(self, wallet, node_port):
        import vm_engine
        from _lite_client import LiteClient
        self._vm_engine = vm_engine
        self.client = LiteClient(wallet, port=int(node_port))
        self.node_port = int(node_port)

    @property
    def address(self):
        return self.client.address

    def _call(self, addr, calldata_hex, value_bis=0.0):
        recipient = self._vm_engine.VM_SINK if value_bis else self.address
        return self.client.send(recipient, float(value_bis), "vm:call", "%s:%s" % (addr, calldata_hex))

    def deploy(self, nseats, sb, bb, max_buyin=None):
        import poker_table
        code = poker_table.build(int(nseats), int(sb), int(bb),
                                 int(max_buyin) if max_buyin else None)
        txid = self.client.send(self.address, 1, "vm:deploy", code.hex())
        return {"txid": txid}

    def vmcall(self, addr, calldata_hex, value_bis=0.0):
        return {"txid": self._call(addr, calldata_hex, float(value_bis))}

    def mine(self, count=1):
        self.client.mine(int(count))
        return {"height": self.client.block_height(), "mined": int(count)}


def make_handler(bus, signer):
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
            path = u.path.rstrip("/")
            if path == "/relay/info":
                info = {"bus": True, "dev_sign": signer is not None}
                if signer is not None:
                    info.update({"address": signer.address, "node_port": signer.node_port,
                                 "custodial_warning": "DEV-SIGN custodial relay — not for real funds"})
                return self._json(200, info)
            if path == "/relay/inbox":
                q = parse_qs(u.query)
                return self._json(200, bus.inbox(q.get("table", [""])[0], int(q.get("since", ["0"])[0])))
            return self._json(404, {"error": "unknown endpoint"})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                b = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                return self._json(400, {"error": "bad json: %s" % e})
            path = self.path.rstrip("/")
            try:
                # --- bus (always available) ---
                if path == "/relay/msg":
                    return self._json(200, bus.push_msg(b.get("table"), b.get("msg")))
                # --- dev-sign only ---
                if path in ("/relay/deploy", "/relay/vmcall", "/relay/mine"):
                    if signer is None:
                        return self._json(404, {"error": "signing disabled; run with --dev-sign (use the "
                                                          "browser wallet instead)"})
                    if path == "/relay/deploy":
                        return self._json(200, signer.deploy(b.get("nseats", 2), b.get("sb", 1),
                                                             b.get("bb", 2), b.get("max_buyin")))
                    if path == "/relay/vmcall":
                        return self._json(200, signer.vmcall(b["address"], b["calldata_hex"],
                                                             b.get("value", 0.0)))
                    if path == "/relay/mine":
                        return self._json(200, signer.mine(b.get("count", 1)))
                return self._json(404, {"error": "unknown endpoint"})
            except KeyError as e:
                return self._json(400, {"error": "missing field %s" % e})
            except Exception as e:
                return self._json(400, {"error": str(e)})

    return H


def main():
    ap = argparse.ArgumentParser(description="Stage-3 poker relay: key-blind deal bus (+ optional dev signer)")
    ap.add_argument("--listen", type=int, default=8096)
    ap.add_argument("--dev-sign", metavar="WALLET.DER", default=None,
                    help="OPT-IN custodial fallback signer for regnet/CI (NOT for real funds)")
    ap.add_argument("--node-port", type=int, default=3030, help="node socket port (only with --dev-sign)")
    args = ap.parse_args()

    bus = Bus()
    signer = None
    if args.dev_sign:
        signer = DevSigner(args.dev_sign, args.node_port)
        print("=" * 72)
        print("  !!  DEV-SIGN CUSTODIAL RELAY ENABLED  !!")
        print("  This relay holds %s and signs txs ON YOUR BEHALF." % args.dev_sign)
        print("  This is the OLD demo crutch — for regnet/CI only. NOT for real funds.")
        print("  Players should sign in the browser wallet (web/wallet/); keys never leave it.")
        print("  wallet address : %s" % signer.address)
        print("  node port      : %d" % args.node_port)
        print("=" * 72)
    else:
        print("poker relay: MESSAGE BUS ONLY (key-blind). Signing happens in the browser wallet.")
        print("  (pass --dev-sign wallet.der to enable the custodial dev/regnet signer.)")

    httpd = ThreadingHTTPServer(("127.0.0.1", args.listen), make_handler(bus, signer))
    print("listening on http://127.0.0.1:%d   bus: /relay/msg + /relay/inbox" % args.listen)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
