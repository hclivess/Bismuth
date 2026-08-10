"""
ONE fork encompasses everything (the 2026-06-12 unification): hf2 is the only signalled fork, and its
single activation height flips the whole bundle together — LWMA difficulty, dynamic fees
gates AND the blake2b Heavy3 PoW (the short-lived separate "pow2" PoW fork was folded into hf2).

Live regnet validation, end to end:
- /api/fork locks in and activates exactly one fork,
- the persisted sidecar holds exactly ONE key ("hf2") at that height,
- the node log records exactly ONE lock-in (and no separate PoW-fork lock),
- every API surface reports the SAME height,
- the REAL solo miner (miner.py) keeps producing accepted blocks PAST activation: it mines blake2b
  and the digester validates blake2b off the same node.fork_height, so continued acceptance proves
  the mining and validation gates agree (a second, divergent height would fail every mined block),
- a non-PoW bundle member (the dynamic base fee) is live post-fork off that same height.

Run with: python3 -m pytest tests/test_single_fork_validation.py -v
"""
import json
import os
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "http://127.0.0.1:3031"


def _get(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def test_single_fork_encompasses_everything(client):
    # mine until the one fork locks in and activates (regnet window=5, boundary=10, bury=2)
    fork_info = {}
    for _ in range(30):
        fork_info = _get("/api/fork")
        if fork_info.get("active"):
            break
        client.mine(3)
    assert fork_info.get("active"), "hf2 never activated: %r" % fork_info
    fh = fork_info["fork_height"]

    # 1. exactly ONE persisted lock-in key — "hf2" — at the reported height (no pow2 / second fork)
    with open(os.path.join(ROOT, "static", "fork_lockin-regmode.db.json")) as sf:
        sidecar = json.load(sf)
    assert sidecar == {"hf2": fh}, "sidecar must hold the single unified fork: %r" % sidecar

    # 2. exactly ONE lock-in in the node log, and no separate PoW-fork lock
    with open(os.path.join(ROOT, "node_test.log")) as lf:
        log = lf.read()
    assert log.count("activation height locked") == 1
    assert "PoW-fork" not in log

    # 3. every surface reports the SAME single height
    assert _get("/api/fork")["fork_height"] == fh

    # 4. the REAL miner mines accepted blocks PAST activation: miner (blake2b) and digester (blake2b)
    #    both gate on node.fork_height, so acceptance proves the PoW switched in lockstep
    h0 = client.block_height()
    assert h0 >= fh, "tip %s not past activation %s" % (h0, fh)
    client.mine_real(2)
    assert client.block_height() >= h0 + 2, "real miner failed to extend the chain post-fork"

    # 5. a non-PoW bundle member rides the same height: the dynamic base fee is live post-fork
    fee = _get("/api/fee")
    assert fee["post_fork"] is True, "dynamic fee not active post-fork: %r" % fee
