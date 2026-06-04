# Ledger / block tests on regnet. Confirms the raw vs JSON command parity and block hashing.
# Run with: python3 -m pytest -v

from hashlib import sha224
from time import sleep


def test_blocklast_json_parity(client):
    client.mine(1)
    raw = client.command("blocklast")
    js = client.command("blocklastjson")
    assert int(raw[0]) == js["block_height"]
    assert isinstance(js["block_height"], int)
    assert raw[7] == js["block_hash"]
    assert isinstance(js["block_hash"], str)


def test_balance_json_parity(client):
    raw = client.command("balanceget", [client.address])
    js = client.command("balancegetjson", [client.address])
    assert raw[0] == js["balance"]
    assert raw[1] == js["credit"]
    assert raw[2] == js["debit"]
    assert raw[3] == js["fees"]
    assert raw[4] == js["rewards"]
    assert raw[5] == js["balance_no_mempool"]


def test_addlistlim_json_parity(client):
    op, data = "12345", "67890"
    client.mine(1)
    client.send(client.address, 1.0, operation=op, data=data)
    client.mine(1)
    sleep(0.5)
    raw = client.command("addlistlim", [client.address, 1])
    js = client.command("addlistlimjson", [client.address, 1])
    fields = ["block_height", "timestamp", "address", "recipient", "amount", "signature",
              "public_key", "block_hash", "fee", "reward", "operation", "openfield"]
    for i, f in enumerate(fields):
        assert raw[0][i] == js[0][f], f


def test_api_getblockfromhash(client):
    client.mine(1)
    client.send(client.address, 1.0, operation="12345", data="67890")
    client.mine(1)
    sleep(0.5)
    js = client.command("addlistlimjson", [client.address, 1])
    block_hash = js[0]["block_hash"]
    block = client.command("api_getblockfromhash", [block_hash])
    height = str(js[0]["block_height"])
    assert block[height]["block_hash"] == block_hash
    assert len(block[height]["transactions"]) == 2  # coinbase + the send


def test_db_blockhash(client):
    # Reconstruct a block hash from its (single coinbase) transaction and the previous hash.
    client.mine(2)
    sleep(0.5)
    last = client.command("blocklastjson")["block_height"]
    rows = client.command("api_getblocksince", [last - 2])
    prev_hash = rows[0][7]
    db_block_hash = rows[1][7]
    r = rows[1]
    tx_list = [("%.2f" % float(r[1]), r[2], r[3], "0.00000000", r[5], r[6], r[10], r[11])]
    recomputed = sha224((str(tx_list) + prev_hash).encode("utf-8")).hexdigest()
    assert db_block_hash == recomputed
