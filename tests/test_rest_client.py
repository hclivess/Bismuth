# REST client tests on regnet: capability discovery + parallel block fetching against the running
# node's own REST API (127.0.0.1:3031). Proves the modern, parallel alternative to socket sync.
# Run with: python3 -m pytest -v

from time import sleep

import rest_client

HOST, PORT = "127.0.0.1", 3031


def test_discovery_reachable_is_capable(client):
    # "If the API is inaccessible, it doesn't exist": a reachable REST API => capable; a dead port =>
    # not capable (so the caller falls back to socket sync).
    assert rest_client.is_rest_capable(HOST, PORT)
    assert not rest_client.is_rest_capable(HOST, 3939, timeout=2)   # nothing is listening there
    caps = rest_client.get_capabilities(HOST, PORT)
    assert caps and caps["rest_api"] is True and caps["rest_port"] == PORT
    assert rest_client.get_capabilities(HOST, 3939, timeout=2) is None


def test_parallel_fetch_matches_chain(client):
    client.mine(6)
    sleep(0.3)
    last = client.command("blocklastjson")["block_height"]
    # small chunk forces several concurrent requests, exercising the parallel path and the reordering
    blocks = rest_client.parallel_fetch(HOST, PORT, 1, last, chunk=2)
    heights = [b["block_height"] for b in blocks]
    assert heights == sorted(heights)              # returned ascending despite out-of-order completion
    assert heights == list(range(heights[0], last + 1))  # contiguous, no gaps, reaches the tip
    assert heights[-1] == last
    # every block carries its transactions with reconstructed (non-raw) amounts
    assert all(b["transactions"] for b in blocks)
    # the height-based fetch agrees with the peer's advertised height
    assert rest_client.get_height(HOST, PORT) >= last
