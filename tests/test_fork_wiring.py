"""
Auto hard-fork wiring (doc/18) — end-to-end on the live regnet node.

config_custom signals (fork_signal=True, window=5, boundary=10, bury=2): the regnet miner stamps the
hf2 marker into the coinbase nonce, the node reads it back from the chain, and /api/fork reports the
signalling run, lock-in, the deterministic activation height, and that the tip is already past it.
This proves the signal → detect → schedule mechanism is wired and works on real node data.

Run with: python3 -m pytest tests/test_fork_wiring.py -v
"""
import json
import urllib.request
from time import sleep

import pytest


def _get(path):
    r = urllib.request.urlopen("http://127.0.0.1:3031/api" + path, timeout=10)
    return r.getcode(), json.loads(r.read().decode())


def test_fork_signal_locks_in_and_activates(client):
    client.mine(15)                                  # well past the small activation boundary
    sleep(0.4)
    code, body = _get("/fork")
    assert code == 200
    assert body["signalling_run"] >= 5, body         # the miner is stamping the marker
    assert body["locked_in"] is True, body           # a full window of signalled blocks
    assert body["fork_height"] is not None and body["fork_height"] % 10 == 0, body
    assert body["active"] is True, body              # tip is past the activation height


def test_fork_height_is_deterministic_and_stable(client):
    # the same chain yields the same activation height on every call (no drift) — the property that
    # keeps nodes from disagreeing
    _, a = _get("/fork")
    client.mine(2)
    sleep(0.3)
    _, b = _get("/fork")
    assert a["fork_height"] == b["fork_height"], (a, b)
