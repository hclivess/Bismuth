# REST API tests on regnet. The regnet node (started by conftest) has rest_api=True on port 3031.
# Run with: python3 -m pytest -v

import json
import socket
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:3031/api"


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _wait_rest(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.settimeout(0.3)
        try:
            if s.connect_ex(("127.0.0.1", 3031)) == 0:
                return True
        finally:
            s.close()
        time.sleep(0.5)
    return False


def test_rest_api_is_up(client):
    assert _wait_rest(), "REST API did not open port 3031"
    code, body = _get("")
    assert code == 200 and body["name"] == "Bismuth REST API"


def test_rest_status(client):
    client.mine(2)
    code, body = _get("/status")
    assert code == 200
    assert body["regnet"] is True
    assert body["blocks"] >= 1


def test_rest_difficulty(client):
    code, body = _get("/difficulty")
    assert code == 200 and "difficulty" in body


def test_rest_block_by_height(client):
    client.mine(1)
    height = client.command("blocklastjson")["block_height"]
    code, body = _get(f"/block/height/{height}")
    assert code == 200
    assert body["block_height"] == height
    assert body["transactions"]


def test_rest_blocks_since_and_range(client):
    client.mine(5)
    top = client.command("blocklastjson")["block_height"]
    code, body = _get(f"/blocks/since/{top - 3}")
    assert code == 200
    heights = [b["block_height"] for b in body["blocks"]]
    assert heights == sorted(heights)                 # ascending, for ordered apply
    assert all(h > top - 3 for h in heights)
    assert len(heights) >= 3
    code, body = _get(f"/blocks/range/{top - 2}/{top}")
    assert code == 200
    rheights = [b["block_height"] for b in body["blocks"]]
    assert rheights == sorted(rheights)
    assert min(rheights) >= top - 2 and max(rheights) <= top
    assert all(b["transactions"] for b in body["blocks"])


def test_rest_balance(client):
    client.mine(1)
    code, body = _get(f"/balance/{client.address}")
    assert code == 200 and float(body["balance"]) > 0


def test_rest_balance_matches_socket_and_is_cached(client):
    client.mine(2)
    code, body = _get(f"/balance/{client.address}")
    assert code == 200
    # the cached read-side balance must equal the node's authoritative (no-mempool) balance
    socket_bal = client.command("balancegetjson", [client.address])["balance_no_mempool"]
    assert abs(float(body["balance"]) - float(socket_bal)) < 1e-8
    # a second read at the same height returns the identical (cached) value
    _, body2 = _get(f"/balance/{client.address}")
    assert body2["balance"] == body["balance"]


def test_rest_transaction(client):
    client.mine(1)
    txid = client.send(client.address, 1.0)
    client.mine(1)
    time.sleep(0.3)
    code, body = _get(f"/transaction/{txid}")
    assert code == 200
    assert body["recipient"] == client.address
    assert float(body["amount"]) == 1.0


def test_rest_address_transactions(client):
    client.mine(1)
    code, body = _get(f"/address/{client.address}/transactions?limit=5")
    assert code == 200
    assert body["limit"] == 5
    assert isinstance(body["transactions"], list)


def test_rest_mempool_and_peers(client):
    code, body = _get("/mempool")
    assert code == 200 and "transactions" in body
    code, body = _get("/peers")
    assert code == 200 and "peers" in body


def test_rest_unknown_endpoint_404(client):
    try:
        _get("/does-not-exist")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404
