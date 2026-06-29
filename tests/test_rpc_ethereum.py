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
    # gasPrice is now the dynamic base fee (wei-scaled), no longer a hardcoded 0x0
    assert int(_rpc("eth_gasPrice")["result"], 16) >= 0
    assert _rpc("net_peerCount")["result"].startswith("0x")


def test_eth_getblock_and_balance(client):
    h = int(_rpc("eth_blockNumber")["result"], 16)
    blk = _rpc("eth_getBlockByNumber", [hex(h), False])["result"]
    assert blk is not None and int(blk["number"], 16) == h and isinstance(blk["transactions"], list)
    assert blk["parentHash"] is not None and blk["uncles"] == []

    # by-hash round-trips to the same block; tx count matches
    by_hash = _rpc("eth_getBlockByHash", [blk["hash"], False])["result"]
    assert int(by_hash["number"], 16) == h
    cnt = int(_rpc("eth_getBlockTransactionCountByNumber", [hex(h)])["result"], 16)
    assert cnt == len(blk["transactions"])
    assert _rpc("eth_getUncleCountByBlockNumber", [hex(h)])["result"] == "0x0"

    bal = _rpc("eth_getBalance", [client.address, "latest"])
    assert bal["result"].startswith("0x") and int(bal["result"], 16) >= 0


def test_eth_status_txpool_filters(client):
    # tx-count is a COUNT, not a nonce (documented caveat) — still must be hex
    assert _rpc("eth_getTransactionCount", [client.address, "latest"])["result"].startswith("0x")
    syncing = _rpc("eth_syncing")["result"]
    assert syncing is False or "currentBlock" in syncing
    assert _rpc("txpool_status")["result"]["pending"].startswith("0x")
    assert _rpc("eth_accounts")["result"] == []
    assert isinstance(_rpc("eth_mining")["result"], bool)

    # block polling filter: create, then observe new block hashes after mining
    fid = _rpc("eth_newBlockFilter")["result"]
    client.mine(1)
    sleep(0.3)
    changes = _rpc("eth_getFilterChanges", [fid])["result"]
    assert isinstance(changes, list)
    assert _rpc("eth_uninstallFilter", [fid])["result"] is True


def test_eth_unsupported_are_honest(client):
    # EVM-defining methods Bismuth cannot back return a specific -32601 reason
    logs = _rpc("eth_getLogs", [{}])
    assert logs["error"]["code"] == -32601 and "log" in logs["error"]["message"]
    send = _rpc("eth_sendTransaction", [{}])
    assert "keyless" in send["error"]["message"]
