"""
api_sync.sync_segment tests on regnet, against the node's own REST API (3031).

Proves the capability-gated, headers-first fetch returns digester-ready blocks for a good range and
fails SOFT (returns None, never raises) for an unreachable peer or a bad range — so the catch-up
caller can safely try it and fall back to socket sync.

Run with: python3 -m pytest tests/test_api_sync.py -v
"""
import os
from time import sleep

import api_sync

# conftest starts the node's REST on 3031 unless the harness re-ports it.
HOST, DEAD_PORT = "127.0.0.1", 3939
PORT = int(os.environ.get("BISMUTH_REST_API_PORT", "3031"))


def test_sync_segment_returns_digester_ready_blocks(client):
    client.mine(4)
    sleep(0.3)
    last = client.command("blocklastjson")["block_height"]

    data = api_sync.sync_segment(HOST, PORT, 1, last)
    assert data is not None
    # digest_block input shape: a list of blocks, each a list of 8-field consensus tx tuples
    assert len(data) == last
    assert all(isinstance(block, list) and block for block in data)
    assert all(len(tx) == 8 for block in data for tx in block)


def test_sync_segment_fail_soft_on_unreachable_peer():
    # nothing listening on DEAD_PORT -> capability check fails -> None (fall back to socket), no raise
    assert api_sync.sync_segment(HOST, DEAD_PORT, 1, 5, timeout=2) is None


def test_sync_segment_fail_soft_on_bad_range(client):
    assert api_sync.sync_segment(HOST, PORT, 10, 1) is None  # end < start


def test_best_rest_source_picks_reachable(client):
    sleep(0.2)
    best = api_sync.best_rest_source([(HOST, DEAD_PORT), (HOST, PORT)], timeout=3)
    assert best == (HOST, PORT)
    assert api_sync.best_rest_source([(HOST, DEAD_PORT)], timeout=2) is None
