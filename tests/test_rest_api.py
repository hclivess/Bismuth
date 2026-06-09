"""REST API tests against the regnet node's HTTP API (rest_api=True, port 3031)."""
# REST API tests on regnet. The regnet node (started by conftest) has rest_api=True on port 3031.
# Run with: python3 -m pytest -v

import gzip
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


def test_rest_welcome_lists_methods(client):
    # The index is a self-describing welcome page listing the available API methods, served at /api...
    code, body = _get("")
    assert code == 200 and body["name"] == "Bismuth REST API"
    assert "/api/capabilities" in body["endpoints"]
    assert "/api/blocks/range/{start}/{end}" in body["endpoints"]
    # ...and at the bare root too, for convenience.
    with urllib.request.urlopen("http://127.0.0.1:3031/", timeout=10) as r:
        root = json.loads(r.read().decode("utf-8"))
    assert root["name"] == "Bismuth REST API"


def test_rest_status(client):
    client.mine(2)
    code, body = _get("/status")
    assert code == 200
    assert body["regnet"] is True
    assert body["blocks"] >= 1


def test_rest_gzip_compression(client):
    # HTTP-standard transport compression: a client that sends Accept-Encoding: gzip gets a
    # Content-Encoding: gzip body that decompresses to the same JSON. This is the bandwidth win for
    # parallel block fetching, applied at the HTTP layer (not the legacy socket protocol).
    req = urllib.request.Request(BASE + "/status", headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.headers.get("Content-Encoding") == "gzip"
        body = json.loads(gzip.decompress(r.read()).decode("utf-8"))
    assert body["regnet"] is True
    # Backward compatible: with no Accept-Encoding (urllib's default), the body is plain JSON.
    code, plain = _get("/status")
    assert code == 200 and plain["regnet"] is True


def test_rest_compress_override(client):
    # ?compress=none is the documented way to read the raw API even from a gzip-capable client...
    req = urllib.request.Request(BASE + "/status?compress=none", headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.headers.get("Content-Encoding") in (None, "")     # forced plaintext
        assert json.loads(r.read().decode("utf-8"))["regnet"] is True
    # ...and ?compress=gzip forces gzip even when the client didn't advertise Accept-Encoding.
    req = urllib.request.Request(BASE + "/status?compress=gzip")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.headers.get("Content-Encoding") == "gzip"
        assert json.loads(gzip.decompress(r.read()).decode("utf-8"))["regnet"] is True


def test_rest_difficulty(client):
    code, body = _get("/difficulty")
    assert code == 200 and "difficulty" in body


def test_rest_capabilities(client):
    # Capability discovery is over the REST API: reachability of /api/capabilities IS the signal that a
    # peer is REST-capable. It advertises the rest port (for peer block fetching) and the negotiable
    # transport codecs (doc/06). If a peer can't serve this, it simply isn't REST-capable.
    code, body = _get("/capabilities")
    assert code == 200
    assert body["rest_api"] is True
    assert body["rest_port"] == 3031          # the regnet REST port (conftest/config_custom)
    assert body["version"]                    # protocol version string
    assert "zlib" in body["compress"] and "none" in body["compress"]


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
