"""
Bitcoin-compatible JSON-RPC adapter (rpc_bitcoin.py) — end-to-end on the live regnet node.

config_custom enables it (rpc_bitcoin=True, port 8331). These POST JSON-RPC 2.0 the way bitcoind tooling
does and check the core methods map onto Bismuth data.

Run with: python3 -m pytest tests/test_rpc_bitcoin.py -v
"""
import json
import urllib.request
from time import sleep

import pytest


def _rpc(method, params=None):
    body = json.dumps({"method": method, "params": params or [], "id": 1}).encode()
    req = urllib.request.Request("http://127.0.0.1:8331", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read().decode())


def test_core_chain_methods(client):
    client.mine(3)
    sleep(0.3)
    n = _rpc("getblockcount")
    assert n["error"] is None and isinstance(n["result"], int) and n["result"] >= 3

    best = _rpc("getbestblockhash")
    assert isinstance(best["result"], str) and len(best["result"]) >= 16

    info = _rpc("getblockchaininfo")["result"]
    assert info["chain"] == "regtest" and info["blocks"] >= 3 and "bestblockhash" in info


def test_getblockhash_getblock_roundtrip(client):
    h = _rpc("getblockcount")["result"]
    bh = _rpc("getblockhash", [h])["result"]
    assert isinstance(bh, str)
    blk = _rpc("getblock", [bh])["result"]
    assert blk["height"] == h and blk["nTx"] >= 1 and isinstance(blk["tx"], list)


def test_getbalance_and_errors(client):
    bal = _rpc("getbalance", [client.address])
    assert bal["error"] is None and float(bal["result"]) >= 0

    miss = _rpc("nonexistent_method")
    assert miss["result"] is None and miss["error"]["code"] == -32601
