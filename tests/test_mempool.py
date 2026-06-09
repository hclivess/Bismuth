"""Mempool command tests against a regnet node."""
# Mempool tests on regnet.
# Run with: python3 -m pytest -v

import time
from base64 import b64decode

import essentials


def _mempool_sigs(client):
    return [row[4] for row in client.command("mpget")]


def test_api_mempool_shows_pending_tx(client):
    client.mine(2)  # flush
    data = "1" * 30
    client.send(client.address, 1.0, data=data)
    time.sleep(0.3)
    pending = client.command("api_mempool")
    match = [t for t in pending if t[7] == data]
    assert match and float(match[0][3]) == 1.0
    client.mine(2)


def test_mpget_json_parity(client):
    client.mine(2)
    client.send(client.address, 1.0)
    raw = client.command("mpget")
    js = client.command("mpgetjson")
    assert raw[0][0] == js[0]["timestamp"]
    assert raw[0][1] == js[0]["address"]
    assert raw[0][2] == js[0]["recipient"]
    assert raw[0][3] == js[0]["amount"]
    assert raw[0][4] == js[0]["signature"]
    assert (b64decode(raw[0][5]).decode("utf-8").replace("\n", "")
            == b64decode(js[0]["public_key"]).decode("utf-8").replace("\n", ""))
    client.mine(2)


def test_tx_enters_then_confirmed(client):
    client.mine(2)  # flush
    txid = client.send(client.address, 1.0)
    time.sleep(0.3)
    assert any(s[:56] == txid for s in _mempool_sigs(client))
    client.mine(2)
    time.sleep(0.3)
    assert not any(s[:56] == txid for s in _mempool_sigs(client))
    tx = client.command("api_gettransaction", [txid, True])
    assert tx and tx.get("amount")  # now in the ledger


def test_duplicate_not_double_counted(client):
    client.mine(2)
    ts = "%.2f" % time.time()
    signed = essentials.sign_rsa(ts, client.address, client.address, 1.0, "", "",
                                 client.key, client.public_key_b64encoded)
    tx = list(signed)
    client.command("mpinsert", [tx])
    client.command("mpinsert", [tx])  # duplicate submission
    time.sleep(0.3)
    assert _mempool_sigs(client).count(signed[4]) == 1
    client.mine(2)


def test_bad_signature_rejected(client):
    client.mine(2)
    ts = "%.2f" % time.time()
    signed = list(essentials.sign_rsa(ts, client.address, client.address, 1.0, "", "",
                                      client.key, client.public_key_b64encoded))
    sig = signed[4]
    signed[4] = ("B" if sig[0] != "B" else "C") + sig[1:]  # corrupt the signature
    client.command("mpinsert", [signed])
    time.sleep(0.3)
    assert signed[4] not in _mempool_sigs(client)
    client.mine(2)
