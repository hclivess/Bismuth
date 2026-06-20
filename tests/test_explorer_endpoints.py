"""
Explorer REST endpoints on a live regnet node: /api/supply, /api/tokens, /api/token/{name}, /api/nodes.

Run with: python3 -m pytest tests/test_explorer_endpoints.py -v
"""
import json
import time
import urllib.error
import urllib.request
from time import sleep

API = "http://127.0.0.1:3031"


def _get(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def test_supply(client):
    client.mine(3)
    sleep(0.3)
    # the first cold call computes the supply in a background thread -> poll until it's ready
    deadline = time.time() + 10
    d = _get("/api/supply")
    while d.get("circulating") is None and time.time() < deadline:
        sleep(0.3)
        d = _get("/api/supply")
    assert d["height"] >= 1
    assert float(d["circulating"]) > 0          # coins exist and are summed


def test_nodes_structure(client):
    d = _get("/api/nodes")
    assert isinstance(d["nodes"], list)
    assert d["count"] == len(d["nodes"])
    assert "tip" in d and "consensus" in d       # network-browse shape


def test_tokens_and_detail(client):
    # issue a token to self; mining processes it via the digest's tokens_update
    client.send(client.address, 1, "token:issue", "tokx:1000000")
    client.mine(3)
    sleep(0.6)
    names = [t["token"] for t in _get("/api/tokens")["tokens"]]
    assert "tokx" in names, names
    detail = _get("/api/token/tokx")
    assert detail["supply"] == 1000000
    assert detail["holder_count"] >= 1
    assert any(h["balance"] == 1000000 for h in detail["holders"])


def test_unknown_token_404(client):
    try:
        _get("/api/token/does_not_exist_zzz")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_token_tx_for_address(client):
    # /api/token/tx/<address>: token transfers (sent or received) for an address, each a dict
    # {token, block_height, timestamp, sender, recipient, amount, signature}
    recipient = "deadbeef" * 7                      # a valid 56-hex (RSA-format) recipient address
    client.send(client.address, 1, "token:issue", "txtok:1000000")
    client.mine(3)
    sleep(0.6)
    client.send(recipient, 1, "token:transfer", "txtok:250")
    client.mine(3)
    sleep(0.6)

    # sender side
    d = _get("/api/token/tx/%s" % client.address)
    rows = d["transactions"]
    match = [r for r in rows if r["token"] == "txtok" and r["recipient"] == recipient and r["amount"] == 250]
    assert match, "transfer not found in sender's token txs: %s" % rows[:5]
    row = match[0]
    assert row["token"] == "txtok" and row["sender"] == client.address
    assert row["recipient"] == recipient and row["amount"] == 250
    assert isinstance(row["block_height"], int) and row["block_height"] > 0   # block height present
    assert row["timestamp"]                             # timestamp present (added to the journal)
    assert row["signature"]                             # signature/txid present

    # recipient side: the same transfer shows up
    d2 = _get("/api/token/tx/%s" % recipient)
    assert any(r["token"] == "txtok" and r["sender"] == client.address and r["amount"] == 250
               for r in d2["transactions"]), d2["transactions"][:5]

    # an address with no token activity returns an empty list (handled, not 404)
    d3 = _get("/api/token/tx/%s" % ("cafe" * 14))
    assert d3["transactions"] == [] and d3["count"] == 0
