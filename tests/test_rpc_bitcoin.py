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


def test_header_and_chain_reads(client):
    client.mine(2)
    sleep(0.3)
    h = _rpc("getblockcount")["result"]
    bh = _rpc("getblockhash", [h])["result"]
    hdr = _rpc("getblockheader", [bh])["result"]
    assert hdr["height"] == h and hdr["hash"] == bh and hdr["confirmations"] >= 1

    tips = _rpc("getchaintips")["result"]
    assert tips[0]["status"] == "active" and tips[0]["height"] == h

    info = _rpc("getblockchaininfo")["result"]
    assert info["headers"] == info["blocks"] and info["pruned"] is False


def test_mempool_mining_network_reads(client):
    mi = _rpc("getmempoolinfo")["result"]
    assert "size" in mi and "bytes" in mi and mi["loaded"] is True
    assert isinstance(_rpc("getrawmempool")["result"], list)

    mining = _rpc("getmininginfo")["result"]
    assert mining["blocks"] >= 1 and "networkhashps" in mining and "difficulty" in mining

    net = _rpc("getnetworkinfo")["result"]
    assert net["connections"] >= 0 and "relayfee" in net
    assert isinstance(_rpc("getpeerinfo")["result"], list)

    fee = _rpc("estimatesmartfee", [6])["result"]
    assert float(fee["feerate"]) >= 0


def test_util_and_unsupported(client):
    good = _rpc("validateaddress", [client.address])["result"]
    assert good["isvalid"] is True
    assert _rpc("validateaddress", ["nope"])["result"]["isvalid"] is False

    # architecturally-impossible methods return a specific -32601 reason, not a bare "not found"
    utxo = _rpc("gettxout", ["x", 0])
    assert utxo["error"]["code"] == -32601 and "UTXO" in utxo["error"]["message"]

    helped = _rpc("help")["result"]
    assert "getblock" in helped["supported"] and "gettxout" in helped["unsupported"]
