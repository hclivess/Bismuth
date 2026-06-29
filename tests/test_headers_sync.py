"""
Headers-first / faithful parallel-sync tests on regnet, against the node's own REST API (3031).

Proves the Bitcoin-style quick-sync path is CONSENSUS-FAITHFUL:
  * /api/headers/range gives a contiguous header chain (height + block_hash) — the cheap first pass.
  * /api/blocks/range?format=sync gives digester-ready tuples whose public key is preserved (NOT
    base64-decoded like the display API), so every signed tx still verifies against its signature and
    each body matches the header's block_hash. This is the property that makes parallel API sync safe
    to feed to the digester.

Run with: python3 -m pytest tests/test_headers_sync.py -v
"""
from time import sleep

import bismuth_serialize
import rest_client
import vm_engine
from polysign.signerfactory import SignerFactory
from quantizer import quantize_two, quantize_eight

HOST, PORT = "127.0.0.1", 3031


def _verify_faithful_tx(tx):
    """Re-derive the signed buffer from a ?format=sync tuple and verify it. Raises if the bytes the
    signature commits to (notably the public key and amount) were not reproduced exactly."""
    ts = "%.2f" % quantize_two(tx[0])
    amt = "%.8f" % quantize_eight(tx[3])
    buffer = bismuth_serialize.signature_buffer(ts, tx[1], tx[2], amt, tx[6], tx[7])
    SignerFactory.verify_bis_signature(tx[4], tx[5], buffer, tx[1])  # raises on mismatch


def test_headers_chain_is_contiguous(client):
    client.mine(5)
    sleep(0.3)
    last = client.command("blocklastjson")["block_height"]
    headers = rest_client.fetch_headers(HOST, PORT, 1, last)
    assert rest_client.headers_are_contiguous(headers, 1)
    assert [h["block_height"] for h in headers] == list(range(1, last + 1))
    assert all(h["block_hash"] for h in headers)


def test_faithful_sync_bodies_match_headers_and_verify(client):
    client.mine(4)
    sleep(0.3)
    last = client.command("blocklastjson")["block_height"]

    headers = rest_client.fetch_headers(HOST, PORT, 1, last)
    hash_by_height = {h["block_height"]: h["block_hash"] for h in headers}

    # small chunk -> several concurrent requests, exercising the parallel reorder path
    blocks = rest_client.parallel_fetch_sync(HOST, PORT, 1, last, chunk=2)
    assert [b["block_height"] for b in blocks] == list(range(1, last + 1))

    verified = 0
    for b in blocks:
        # every body carries the same block_hash the header advertised (headers-first verification)
        assert b["block_hash"] == hash_by_height[b["block_height"]]
        # consensus-faithful: every real signed tx verifies (pubkey preserved + amount reconstructed)
        if b["block_height"] >= 2:
            for tx in b["transactions"]:
                # skip the post-fork coinbase: its signature/public_key slots carry the mining header
                # (nonce + "vmsr"<state root>, doc/41), not a real signature/key — it is PoW-authorized.
                if tx[4] and tx[5] and vm_engine.extract_state_root(tx[5]) is None:
                    _verify_faithful_tx(tx)
                    verified += 1
    assert verified > 0, "expected at least one signed tx to verify against the faithful serialization"


def test_blocks_to_digester_shape(client):
    client.mine(2)
    sleep(0.3)
    last = client.command("blocklastjson")["block_height"]
    blocks = rest_client.parallel_fetch_sync(HOST, PORT, 1, last, chunk=2)
    data = rest_client.blocks_to_digester(blocks)
    # digest_block consumes a list of blocks, each a list of tx tuples (8 consensus fields each)
    assert len(data) == len(blocks)
    assert all(isinstance(blk, list) for blk in data)
    assert all(len(tx) == 8 for blk in data for tx in blk)
