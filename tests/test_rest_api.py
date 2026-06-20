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


def test_rest_block_reads_post_fork_use_lmdb(client):
    # doc/26 stage 3: post-fork the REST block-BODY reads come from the LMDB block_store (regnet has
    # block_store=True), with a SQLite fallback. Mine until hf2 is active (so node.last_block >=
    # node.fork_height -> the seam picks the LMDB backend), then read blocks every which way and assert
    # they're correct. This deterministically pins the LMDB-served path; data parity with SQLite is proven
    # separately by storage_backend.cross_check. (Kept LAST in this file: it advances the chain past the
    # fork, and we don't want that burst of mining to race the timing-sensitive tx-inclusion test above.)
    assert _wait_rest()
    _, fk = _get("/fork")
    deadline = time.time() + 40
    while not fk.get("active") and time.time() < deadline:
        client.mine(3)
        _, fk = _get("/fork")
    assert fk.get("active") is True, fk

    tip = client.command("blocklastjson")["block_height"]

    # by height (LMDB post-fork)
    code, body = _get(f"/block/height/{tip}")
    assert code == 200 and body["block_height"] == tip and body["transactions"]

    # by hash: the hash comes from the (SQLite) header endpoint, the body is read by hash (LMDB) — and the
    # two body reads must be byte-identical (same source, same format_raw_tx)
    _, hdr = _get(f"/headers/range/{tip}/{tip}")
    bhash = hdr["headers"][0]["block_hash"]
    code, bh = _get(f"/block/hash/{bhash}")
    assert code == 200 and bh["block_height"] == tip
    assert bh["transactions"] == body["transactions"]

    # range fully within the store
    code, rng = _get(f"/blocks/range/{tip - 2}/{tip}")
    assert code == 200
    rheights = [b["block_height"] for b in rng["blocks"]]
    assert rheights == sorted(rheights) and max(rheights) == tip
    assert all(b["transactions"] for b in rng["blocks"])


# --- /api/proxy: same-origin relay so an https explorer can browse an http node (no mixed-content) ---
# The relay fetches a *target* node's read-only /api server-side and returns it. It is read-only,
# GET-only, /api-paths-only, and SSRF-guarded (only public hosts; loopback/private/metadata refused).

from urllib.parse import quote as _q


def _proxy_get(target):
    """GET /api/proxy?target=<url>, returning (status, body) even for 4xx/5xx (no raise)."""
    url = BASE + "/proxy?target=" + _q(target, safe="")
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_rest_proxy_listed_in_index(client):
    assert _wait_rest()
    _, body = _get("")
    assert "/api/proxy?target={url}" in body["endpoints"]


def test_rest_proxy_requires_target(client):
    try:
        with urllib.request.urlopen(BASE + "/proxy", timeout=10) as r:
            code, body = r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        code, body = e.code, json.loads(e.read().decode("utf-8"))
    assert code == 400 and body["error"] == "bad_request"


def test_rest_proxy_rejects_non_api_path(client):
    # Never an arbitrary-URL fetcher: only /api paths are relayable.
    code, body = _proxy_get("http://93.184.216.34/etc/passwd")
    assert code == 403 and body["error"] == "forbidden"


def test_rest_proxy_rejects_non_http_scheme(client):
    code, body = _proxy_get("file:///etc/passwd")
    assert code == 400 and body["error"] == "bad_request"


def test_rest_proxy_blocks_loopback_ssrf(client):
    # The guard must refuse to let the node be used as an SSRF pivot to loopback...
    code, body = _proxy_get("http://127.0.0.1:3031/api/status")
    assert code == 403 and body["error"] == "forbidden"


def test_rest_proxy_blocks_private_and_metadata_ssrf(client):
    # ...private ranges and the cloud-metadata link-local IP.
    for tgt in ("http://10.0.0.1:5659/api/status",
                "http://192.168.1.1/api/status",
                "http://169.254.169.254/api/status"):
        code, body = _proxy_get(tgt)
        assert code == 403 and body["error"] == "forbidden", tgt


def test_rest_proxy_happy_path_public(client):
    # Full relay against a real public http node. Network-gated: skipped when offline so CI stays green.
    import pytest
    target = "http://185.100.232.5:5659/api/status"
    try:
        code, body = _proxy_get(target)
    except urllib.error.URLError as e:
        pytest.skip("public test node unreachable: %s" % e)
    if code == 502:
        pytest.skip("relay could not reach public test node: %s" % body)
    assert code == 200
    # The relayed payload is the *target* node's status, returned same-origin to the browser.
    assert body.get("node_version") and "blocks" in body


# --- /api/nodes dedup: the live connection pool is keyed host:port while opinion/version/reputation are
# keyed by bare host; unioning them verbatim listed every connected peer twice (once connected-but-blank,
# once known-with-version). _nodes() must fold them into one row per host. Hermetic (no running node). ---

def test_nodes_dedup_merges_host_and_hostport():
    import types
    import rest_api

    class FakePeers:
        peer_opinion_dict = {"185.100.232.5": 4862899, "185.184.192.210": 4862899}
        ip_to_mainnet = {"185.100.232.5": "mainnet0023", "185.184.192.210": "mainnet0022"}
        _connection_pool_set = {"185.100.232.5:5658", "185.184.192.210:5658", "207.246.101.70:5658"}
        consensus = 4862899
        consensus_percentage = 100.0
        _rep = {"185.100.232.5": 20, "185.184.192.210": 100}

        def reputation(self, ip):
            return self._rep.get(ip, 0)

        def is_banned(self, ip):
            return False

        def is_whitelisted(self, ip):
            return False

    node = types.SimpleNamespace(peers=FakePeers(), hdd_block=4862899)
    Handler = rest_api._make_handler(node)
    res = Handler.__new__(Handler)._nodes()
    ips = [n["ip"] for n in res["nodes"]]

    # one row per host, no leftover host:port rows
    assert len(ips) == len(set(ips)), ips
    assert not any(":" in i and i.rsplit(":", 1)[-1].isdigit() for i in ips), ips
    # the connected peer carries its version + reputation (the whole point)
    m = {n["ip"]: n for n in res["nodes"]}
    assert m["185.100.232.5"]["connected"] is True
    assert m["185.100.232.5"]["version"] == "mainnet0023"
    assert m["185.100.232.5"]["reputation"] == 20
    # a pool-only peer (no opinion/version yet) still shows, as connected with blanks
    assert m["207.246.101.70"]["connected"] is True and m["207.246.101.70"]["version"] is None
