"""
Ethereum/ERC compatibility shim (rpc_ethereum.py) — end-to-end on the live regnet node.

config_custom enables it (rpc_ethereum=True, port 8546). These POST eth_* JSON-RPC and check the
hex-encoded chain reads. It's a bounded shim (Bismuth addresses ≠ eth addresses, no EVM), so the tests
cover what it actually provides: chain height/blocks/balances, hex-encoded for eth clients.

Run with: python3 -m pytest tests/test_rpc_ethereum.py -v
"""
import json
import urllib.request
from time import sleep

import pytest


def _rpc(method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or [], "id": 1}).encode()
    req = urllib.request.Request("http://127.0.0.1:8546", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read().decode())


def test_eth_chain_reads(client):
    client.mine(2)
    sleep(0.3)
    bn = _rpc("eth_blockNumber")
    assert bn["result"].startswith("0x") and int(bn["result"], 16) >= 2

    assert _rpc("eth_chainId")["result"] == hex(0xB15)
    assert _rpc("net_version")["result"] == str(0xB15)
    assert _rpc("web3_clientVersion")["result"].startswith("Bismuth/")
    assert _rpc("eth_gasPrice")["result"] == "0x0"


def test_eth_getblock_and_balance(client):
    h = int(_rpc("eth_blockNumber")["result"], 16)
    blk = _rpc("eth_getBlockByNumber", [hex(h), False])["result"]
    assert blk is not None and int(blk["number"], 16) == h and isinstance(blk["transactions"], list)

    bal = _rpc("eth_getBalance", [client.address, "latest"])
    assert bal["result"].startswith("0x") and int(bal["result"], 16) >= 0
