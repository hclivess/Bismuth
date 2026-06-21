"""Explorer /api/stats/* endpoints on the regnet node (rest_api=True, port 3031). Read-only, consensus-
neutral aggregates for the stats page (doc/15)."""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:3031/api"


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


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
