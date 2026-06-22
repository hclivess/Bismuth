"""Explorer /api/stats/* endpoints on the regnet node (rest_api=True, port 3031). Read-only, consensus-
neutral aggregates for the stats page (doc/15)."""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:3031/api"


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _poll_ok(path, tries=30):
    """Heavy stats are background-cached + serialized (one heavy scan at a time); poll briefly for 'ok'."""
    body = None
    for _ in range(tries):
        _, body = _get(path)
        if body.get("status") == "ok":
            return body
        time.sleep(0.5)
    return body


def test_stats_summary(client):
    client.mine(2)
    code, body = _get("/stats/summary")
    assert code == 200
    for k in ("height", "difficulty", "connections", "mempool"):
        assert k in body, (k, body)
    assert body["height"] >= 1


def test_stats_tx_per_month(client):
    client.mine(2)
    body = None
    for _ in range(20):                       # the histogram is background-computed; poll briefly for 'ok'
        _, body = _get("/stats/tx_per_month")
        if body.get("status") == "ok":
            break
        time.sleep(0.5)
    assert body["status"] in ("ok", "computing")
    if body["status"] == "ok":
        assert isinstance(body["months"], list)
        for m in body["months"]:
            assert "month" in m and "count" in m
            assert isinstance(m["count"], int)


def test_stats_monthly(client):
    client.mine(2)
    body = _poll_ok("/stats/monthly")
    assert body["status"] in ("ok", "computing")
    if body["status"] == "ok":
        assert isinstance(body["months"], list)
        assert {"txs", "volume", "fees", "emission"} <= set(body.get("totals", {}))
        for m in body["months"]:
            for k in ("month", "txs", "volume", "fees", "emission", "issued", "active"):
                assert k in m, (k, m)
        # issued is cumulative block-reward emission -> non-decreasing
        iss = [m["issued"] for m in body["months"]]
        assert iss == sorted(iss)


def test_stats_new_addresses(client):
    client.mine(2)
    body = _poll_ok("/stats/new_addresses")
    assert body["status"] in ("ok", "computing")
    if body["status"] == "ok":
        assert isinstance(body["months"], list)
        assert body.get("total", 0) >= 1            # at least the mining/genesis addresses exist
        for m in body["months"]:
            assert "month" in m and isinstance(m["count"], int)


def test_stats_rich_list(client):
    client.mine(2)
    body = _poll_ok("/stats/rich_list?top=10")
    assert body["status"] in ("ok", "computing")
    if body["status"] == "ok":
        assert isinstance(body["rich"], list) and len(body["rich"]) <= 10
        prev = None
        for r in body["rich"]:
            assert "rank" in r and "address" in r and "balance" in r
            if prev is not None:
                assert r["balance"] <= prev          # ranked descending by balance
            prev = r["balance"]


def test_stats_top_miners(client):
    client.mine(3)
    body = _poll_ok("/stats/top_miners?top=10")
    assert body["status"] in ("ok", "computing")
    if body["status"] == "ok":
        assert isinstance(body["miners"], list)
        prev = None
        for m in body["miners"]:
            for k in ("rank", "address", "blocks", "share", "reward"):
                assert k in m, (k, m)
            if prev is not None:
                assert m["blocks"] <= prev            # ranked descending by blocks mined
            prev = m["blocks"]


def test_stats_largest_txs(client):
    client.mine(2)
    body = _poll_ok("/stats/largest_txs?top=10")
    assert body["status"] in ("ok", "computing")
    if body["status"] == "ok":
        assert isinstance(body["txs"], list) and len(body["txs"]) <= 10
        prev = None
        for t in body["txs"]:
            for k in ("height", "sender", "recipient", "amount", "signature"):
                assert k in t, (k, t)
            if prev is not None:
                assert t["amount"] <= prev            # ranked descending by amount
            prev = t["amount"]


def test_stats_market_disabled_in_tests(client):
    # the regnet test config sets rest_api_market=False, so no external coingecko call is made
    code, body = _get("/stats/market")
    assert code == 200 and body["status"] == "disabled"


def test_stats_difficulty(client):
    client.mine(3)
    code, body = _get("/stats/difficulty")
    assert code == 200 and isinstance(body.get("series"), list)
    for s in body["series"][:5]:
        assert "height" in s and "difficulty" in s


def test_stats_geo_shape(client):
    # best-effort + cached; on regnet there are no public peers, so it stays computing/empty — we only
    # assert the SHAPE is well-formed (the frontend tolerates computing/disabled/ok).
    code, body = _get("/stats/geo")
    assert code == 200
    assert body["status"] in ("ok", "computing", "disabled")
    assert isinstance(body.get("points", []), list)
    assert isinstance(body.get("countries", []), list)
