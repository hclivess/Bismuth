# Node command tests on regnet.
# Run with: python3 -m pytest -v

import hashlib
from base64 import b64decode
from time import sleep


def test_port_regnet(client):
    assert int(client.command("portget")["port"]) == 3030


def test_diff_json_parity(client):
    client.mine(1)
    raw = client.command("difflast")
    js = client.command("difflastjson")
    assert raw[0] == js["block"]
    assert raw[1] == js["difficulty"]


def test_keygen_json_parity(client):
    raw = client.command("keygen")
    js = client.command("keygenjson")
    assert len(raw[1]) > 0 and len(raw[2]) > 0
    assert len(raw[1]) == len(js["public_key"])
    assert len(raw[2]) == len(js["address"])


def test_api_config(client):
    cfg = client.command("api_getconfig")
    assert cfg["regnet"] is True
    assert int(cfg["port"]) == 3030


def test_api_getaddresssince(client):
    client.mine(1)
    client.send(client.address, 1.0)
    client.mine(10)
    sleep(0.5)
    last = client.command("blocklastjson")["block_height"]
    data = client.command("api_getaddresssince", [last - 10, 8, client.address])
    assert len(data["transactions"]) == 3  # reward + sent tx + another reward


def test_api_getblockssince(client):
    client.mine(1)
    data, amount = "1234567890", 1.5
    client.send(client.address, amount, data=data)
    client.mine(10)
    sleep(0.5)
    last = client.command("blocklastjson")["block_height"]
    blocks = client.command("api_getblocksince", [last - 10])
    assert len(blocks) == 11
    assert blocks[0][11] == data
    assert float(blocks[0][4]) == amount


def test_add_validate(client):
    assert client.command("addvalidate", [client.address]) == "valid"
    assert client.command("addvalidate", ["not-valid"]) == "invalid"


def test_pubkey_address(client):
    client.mine(1)
    sleep(0.3)
    last = client.command("blocklastjson")
    pubkey = b64decode(last["public_key"]).decode("utf-8")
    assert hashlib.sha224(pubkey.encode("utf-8")).hexdigest() == client.address
