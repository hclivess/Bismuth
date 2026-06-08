"""
Real solo miner (miner.py) on regnet — the mainnet code path, driven via the regtest_mine command.

It must: build a block that EMBEDS pending mempool transactions, stamp the hf2 readiness signal into the
coinbase, mine the real Heavy3 PoW, and digest it. The PoW is DUAL-ALGO — today's sha224-anneal and the
modernised blake2b-anneal (doc/18-D) — switched by on-chain signalling.

Run with: python3 -m pytest tests/test_miner.py -v
"""
import json
import urllib.request
from time import sleep

API = "http://127.0.0.1:3031"


def _get(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def test_miner_embeds_mempool_tx_and_mines(client):
    client.mine(2)
    h0 = client.block_height()
    client.send(client.address, 1.0)            # a pending mempool transaction
    sleep(0.5)
    client.mine_real(1)                          # the REAL miner builds + mines (Heavy3) + digests a block
    assert client.block_height() == h0 + 1, "miner did not advance the chain"

    blk = _get("/api/block/height/%d" % client.block_height())
    cb = [t for t in blk["transactions"] if float(t.get("reward") or 0) != 0]
    assert cb, "mined block has no coinbase"
    amounts = [float(t.get("amount") or 0) for t in blk["transactions"]]
    assert any(abs(a - 1.0) < 1e-8 for a in amounts), "miner did not embed the pending mempool tx"


def test_miner_coinbase_carries_hf2_signal(client):
    client.mine_real(1)
    blk = _get("/api/block/height/%d" % client.block_height())
    cb = [t for t in blk["transactions"] if float(t.get("reward") or 0) != 0]
    of = (cb[0].get("openfield") if cb else "") or ""
    assert "hf2" in of, "coinbase missing the hf2 readiness signal: %r" % of[:24]


def test_dual_algo_pow_switches(client):
    # the node (which holds the Heavy3 mmap) runs diffme_heavy3 both ways over a few samples
    old_s, new_s = client.command("regtest_powcheck").split("|")
    old_vals = [int(x) for x in old_s.split(",")]
    new_vals = [int(x) for x in new_s.split(",")]
    assert all(v >= 0 for v in old_vals + new_vals), "a dual-algo PoW path failed to run"
    assert old_vals != new_vals, "sha224 and blake2b PoW gave identical results — algo not switching"
