# ApiHandler (api_*) tests on regnet.
# Run with: python3 -m pytest -v

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


def test_tokensget_and_aliasget_no_crash(client):
    # tokensget exercises the fixed tokens_user SQL; both must return cleanly.
    assert client.command("tokensget", [client.address]) == []
    client.command("aliasget", [client.address])
