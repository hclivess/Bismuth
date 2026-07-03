"""End-to-end Optipoolware integration against a live regnet node (post-fork).

Drives the pool's ACTUAL functions — no reimplementation — and asserts the node accepts both
consensus-critical outputs and the chain settles them:

  1. process_share -> builds the hf2 §2.C COMPACT coinbase block (empty sig+pubkey) and submits it
                      via POST /api/block; the node must accept it and credit the pool address.
  2. payout        -> builds + RSA-signs the payout tx using the node's LIVE /api/fee base_fee and
                      submits it via POST /api/transaction; it must confirm on-chain to the recipient.

GATED (spawns a node) behind BISMUTH_RUN_MULTINODE, like test_multinode_integration. Run it ALONE so
it owns the regnet socket/REST ports (3030/3031) — the normal suite skips it:

    BISMUTH_RUN_MULTINODE=1 python3 -m pytest tests/test_pool_integration.py -q -s
"""
import json
import math
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REST = "http://127.0.0.1:3031/api"

# files the pool / node / Heavy3 generate in the repo root that the test must clean up afterwards
_GEN_FILES = ["privkey.der", "pubkey.der", "shares.db", "archive.db", "pool.log",
              "heavy3a.bin", "config_custom.txt", "node_pool_it.log"]


def _nget(path, timeout=10):
    with urllib.request.urlopen(REST + path, timeout=timeout) as r:
        return json.load(r)


def _port_open(p):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", p)) == 0
    finally:
        s.close()


def _clean_state():
    for name in ("static/regmode.db", "static/index_reg.db", "static/regmode.db-shm",
                 "static/regmode.db-wal", "static/index_reg.db-shm", "static/index_reg.db-wal"):
        try:
            os.remove(os.path.join(ROOT, name))
        except OSError:
            pass
    for d in ("static/blockstore", "static/balanceindex", "static/txidindex", "static/vmstate",
              "static/pkregistry", "static/tokenindex-regmode.db"):
        shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)


@pytest.mark.skipif(not os.environ.get("BISMUTH_RUN_MULTINODE"),
                    reason="spawns a regnet node; set BISMUTH_RUN_MULTINODE=1 and run this file alone")
def test_pool_block_and_payout():
    os.chdir(ROOT)
    for p in (ROOT, os.path.join(ROOT, "tests"), os.path.join(ROOT, "pool")):
        if p not in sys.path:
            sys.path.insert(0, p)

    node = None
    pool = None
    try:
        # --- start a fresh regnet node (socket 3030, REST 3031 from config_custom) ---
        _clean_state()
        shutil.copy(os.path.join(ROOT, "tests/config_custom.txt"), os.path.join(ROOT, "config_custom.txt"))
        logf = open(os.path.join(ROOT, "node_pool_it.log"), "w")
        node = subprocess.Popen([sys.executable, "node.py", "regnet2"], cwd=ROOT,
                                stdout=logf, stderr=subprocess.STDOUT)
        deadline = time.time() + 90
        while time.time() < deadline:
            if _port_open(3030) and _port_open(3031):
                break
            if node.poll() is not None:
                raise RuntimeError("node died on startup; see node_pool_it.log")
            time.sleep(0.5)
        else:
            raise RuntimeError("node did not open 3030+3031 in time")
        time.sleep(1.0)

        # --- mine past the hf2 fork using the node's own miner ---
        from _lite_client import LiteClient
        c = LiteClient(os.path.join(ROOT, "wallet.der"), port=3030)
        for _ in range(8):
            fh = _nget("/fork").get("fork_height")
            if fh is not None and (c.block_height() + 1) >= int(fh):
                break
            c.mine(5)
            time.sleep(0.3)
        fh = _nget("/fork").get("fork_height")
        assert fh is not None and (c.block_height() + 1) >= int(fh), "regnet did not cross the fork"

        # --- import the pool (config_custom -> REST 3031) and prime its work globals like worker() ---
        import optipoolware as pool
        pool.mining.mining_open()
        st = _nget("/status")
        pool.new_hash = st["last_block_hash"]
        pool.new_diff = math.floor(float(_nget("/difficulty")["difficulty"]))
        pool._refresh_fork_state(st["blocks"])
        assert pool.new_pow is True, "pool did not observe post-fork state"

        # --- mine a valid PoW nonce. doc/41: post-fork the miner grinds a BARE nonce (the cb_prefix
        # state-root commitment rides in the coinbase public_key slot, added by the pool, NOT in the PoW) ---
        nonce = None
        for _ in range(2_000_000):
            cand = "%016x" % random.getrandbits(64)
            if pool.mining.diffme_heavy3(pool.address, cand, pool.new_hash, new_pow=pool.new_pow) >= pool.new_diff:
                nonce = cand
                break
        assert nonce is not None, "no PoW nonce found at regnet difficulty"

        # === 1. process_share: compact-coinbase block submitted + accepted ===
        MINER = "a" * 56                                   # valid s_test address; the payout recipient
        tip_before = c.block_height()
        pool_bal_before = c.balance(pool.address)
        sh = {"block_timestamp": "%.2f" % time.time(), "nonce": nonce, "blockhash": pool.new_hash,
              "sdiff": pool.new_diff, "rate": 0, "worker_base": "w", "workers": 1, "worker_num": "1"}
        res = pool.process_share(MINER, sh)
        assert res.get("block_found"), "share did not meet network difficulty: %r" % res
        ok = False
        for _ in range(40):
            if c.block_height() > tip_before:
                ok = True
                break
            time.sleep(0.25)
        assert ok, "node did not accept the pool's compact-coinbase block (tip stalled)"
        assert c.balance(pool.address) > pool_bal_before, "compact coinbase did not credit the pool"

        # === 2. payout: RSA-signed tx with the live fee, confirmed on-chain ===
        import sqlite3
        shares = sqlite3.connect(os.path.join(ROOT, "shares.db"))
        shares.text_factory = str
        cur = shares.cursor()
        cur.execute("INSERT INTO shares VALUES (?,?,?,?,?,?,?,?)",
                    (MINER, 1, "%.2f" % (time.time() - 5), "0", 0, "w", 1, "w1"))
        shares.commit()
        shares.close()
        miner_before = c.balance(MINER)
        pool.payout(0.00000001, 0, 0)                      # tiny threshold, no pool fee -> pay MINER
        # the mempool merge is async, so POLL: mine until the payout confirms (no fixed-sleep flake)
        settled = False
        deadline = time.time() + 25
        while time.time() < deadline:
            if c.balance(MINER) > miner_before:
                settled = True
                break
            c.mine(1)
            time.sleep(0.4)
        assert settled, "payout did not settle to the miner within the deadline"
    finally:
        if pool is not None:
            try:
                pool.mining.mining_close()
            except Exception:
                pass
        if node is not None:
            node.terminate()
            try:
                node.wait(timeout=10)
            except Exception:
                node.kill()
        _clean_state()
        for f in _GEN_FILES:
            try:
                os.remove(os.path.join(ROOT, f))
            except OSError:
                pass
