"""ApiHandler (api_*) command tests against a regnet node."""
# ApiHandler (api_*) tests on regnet.
# Run with: python3 -m pytest -v

import json
from time import sleep


def test_statusjson_shape(client):
    st = client.command("statusjson")
    assert isinstance(st, dict)
    assert "blocks" in st and "protocolversion" in st and "difficulty" in st


def test_api_ping(client):
    assert client.command("api_ping") == "api_pong"


def test_api_getbalance(client):
    client.mine(1)
    balance = client.command("api_getbalance", [[client.address], 1])
    assert float(balance) > 0


def test_api_getaddressinfo(client):
    client.mine(1)
    client.send(client.address, 1.0)  # an outgoing (reward=0) tx puts the pubkey on record
    client.mine(1)
    sleep(0.5)
    info = client.command("api_getaddressinfo", [client.address])
    assert info["known"] is True
    assert info["pubkey"]


def test_api_gettransaction(client):
    client.mine(1)
    txid = client.send(client.address, 1.0)
    client.mine(1)
    sleep(0.5)
    tx = client.command("api_gettransaction", [txid, True])
    assert isinstance(tx, dict)
    assert tx["recipient"] == client.address
    assert float(tx["amount"]) == 1.0


def test_api_getblockfromheight(client):
    client.mine(1)
    height = client.command("blocklastjson")["block_height"]
    block = client.command("api_getblockfromheight", [height])
    assert str(height) in block
    assert block[str(height)]["transactions"]


def test_api_getblockrange_classifies_normal_tx(client):
    # api_getblockrange -> blocktojsondiffs splits each block into a `mining_tx` (reward != 0) and the
    # normal `transactions` (reward == 0). In integer-storage mode this hinges on format_raw_tx
    # returning a NUMERIC reward so `reward == 0` holds; a string '0.00000000' would be != 0 and
    # misclassify the send as a mining tx, dropping it from `transactions`. Locks that path (doc/16).
    client.mine(1)
    start = client.command("blocklastjson")["block_height"]
    marker = "rangeclassify"
    client.send(client.address, 1.0, data=marker)  # an outgoing reward==0 tx
    client.mine(2)
    sleep(0.5)
    blocks = client.command("api_getblockrange", [start, 5])
    if isinstance(blocks, str):  # api_getblockrange replies with a json.dumps string
        blocks = json.loads(blocks)
    assert blocks, "api_getblockrange returned no blocks"
    found = False
    for block in blocks.values():
        assert "mining_tx" in block  # every block keeps its coinbase as the mining tx
        for tx in block.get("transactions", []):
            if tx.get("openfield") == marker:
                found = True
    assert found, "sent (reward==0) tx was misclassified and dropped from normal transactions"


def test_tokensget_and_aliasget_no_crash(client):
    # tokensget exercises the fixed tokens_user SQL; both must return cleanly.
    assert client.command("tokensget", [client.address]) == []
    client.command("aliasget", [client.address])
