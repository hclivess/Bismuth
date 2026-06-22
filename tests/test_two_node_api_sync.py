"""
Two-node API-sync harness (doc/17, the "API fork").

Proves a node can catch up the ENTIRE chain from a peer over the REST API alone — no socket sync. Starts
two independent regnet nodes on different ports + isolated data dirs (env-parameterized regnet paths):

    A  socket 4040 / REST 4041   — mines a chain (crossing the regnet hf2 fork window)
    B  socket 4042 / REST 4043   — api_sync=ON, api_sync_source=127.0.0.1:4041 (A's REST)

B starts at genesis with an empty ledger and is expected to reconstruct A's chain purely via
api_sync_worker -> api_sync.sync_segment -> digest_block. Success criteria:
  * B's tip reaches A's tip,
  * EVERY block hash 1..tip is identical between the two independent ledgers, and
  * B did this with block_store + balance_index + txid_index in PRIMARY + parity_strict (from
    config_custom.txt) — so the digester's parity asserts ran on every API-delivered block. Identical
    hashes under strict parity => API sync is a faithful, consensus-identical transport.

This is a heavier integration test that manages its own two node processes; run it on its own:
    python3 -m pytest tests/test_two_node_api_sync.py -v
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

import pytest

pytest.importorskip("lmdb")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _rest(port, path, timeout=8):
    with urllib.request.urlopen("http://127.0.0.1:%d/api/%s" % (port, path), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _tip_via_rest(port):
    try:
        return int(_rest(port, "status").get("blocks", 0) or 0)
    except Exception:
        return -1


def _start_node(datadir, sock_port, rest_port, extra_env):
    os.makedirs(datadir, exist_ok=True)
    env = os.environ.copy()
    env["BISMUTH_REGNET_PORT"] = str(sock_port)
    env["BISMUTH_REST_API_PORT"] = str(rest_port)
    env["BISMUTH_REGNET_DB"] = os.path.join(datadir, "regmode.db")
    env["BISMUTH_REGNET_INDEX"] = os.path.join(datadir, "index_reg.db")
    env["BISMUTH_REGNET_PEERS"] = os.path.join(datadir, "peers_reg.txt")
    env["BISMUTH_MEMPOOL_PATH"] = os.path.join(datadir, "mempool.db")
    env.update(extra_env)
    log = open(os.path.join(datadir, "node.log"), "w")
    proc = subprocess.Popen([sys.executable, "node.py", "regnet2"], cwd=ROOT,
                            stdout=log, stderr=subprocess.STDOUT, env=env)
    return proc, log


def _await(cond, deadline_s, poll=1.0):
    end = time.time() + deadline_s
    while time.time() < end:
        if cond():
            return True
        time.sleep(poll)
    return False


def _block_hashes(ledger_path):
    """{height: block_hash} for every positive-height block in a ledger (read-only)."""
    conn = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=20)
    conn.text_factory = str
    try:
        rows = conn.execute("SELECT block_height, block_hash FROM transactions "
                            "WHERE block_height > 0 GROUP BY block_height ORDER BY block_height").fetchall()
    finally:
        conn.close()
    return {int(h): bh for (h, bh) in rows}


def _tail(path, n=25):
    try:
        with open(path) as f:
            return "".join(f.readlines()[-n:])
    except Exception:
        return "(no log)"


@pytest.mark.skipif(not os.environ.get("BISMUTH_RUN_TWONODE"),
                    reason="heavy two-node harness (spins up 2 extra node processes); "
                           "run explicitly with BISMUTH_RUN_TWONODE=1")
def test_two_node_api_sync(tmp_path):
    # both nodes read ROOT/config_custom.txt (regnet flags: block_store/balance_index=primary/txid_index=
    # primary/parity_strict/vm/shield/token_index). Prod ignores it (BISMUTH_IGNORE_CONFIG_CUSTOM=1).
    if not os.path.exists(os.path.join(ROOT, "config_custom.txt")):
        shutil.copy(os.path.join(ROOT, "tests/config_custom.txt"), os.path.join(ROOT, "config_custom.txt"))

    dir_a, dir_b = str(tmp_path / "a"), str(tmp_path / "b")
    procs = []
    try:
        # --- node A: serve + mine ---
        pa, la = _start_node(dir_a, 4040, 4041, {})
        procs.append((pa, la, os.path.join(dir_a, "node.log")))
        assert _await(lambda: _port_open(4040), 90), "A socket never opened\n" + _tail(os.path.join(dir_a, "node.log"))
        assert _await(lambda: _tip_via_rest(4041) >= 1, 90), "A REST never came up\n" + _tail(os.path.join(dir_a, "node.log"))

        from _lite_client import LiteClient
        ca = LiteClient(os.path.join(ROOT, "wallet.der"), port=4040)
        ca.mine(16)                                   # crosses the regnet hf2 fork (window 5 / boundary 10 / bury 2)
        a_tip = _tip_via_rest(4041)
        assert a_tip >= 14, "A only reached height %s" % a_tip

        # --- node B: empty ledger, catch up from A over REST ONLY ---
        pb, lb = _start_node(dir_b, 4042, 4043,
                             {"BISMUTH_API_SYNC": "1", "BISMUTH_API_SYNC_SOURCE": "127.0.0.1:4041"})
        procs.append((pb, lb, os.path.join(dir_b, "node.log")))
        assert _await(lambda: _port_open(4042), 90), "B socket never opened\n" + _tail(os.path.join(dir_b, "node.log"))
        assert _await(lambda: _tip_via_rest(4043) >= 1, 90), "B REST never came up\n" + _tail(os.path.join(dir_b, "node.log"))

        # B should reconstruct A's chain over REST within the window
        caught = _await(lambda: _tip_via_rest(4043) >= a_tip, 150, poll=2.0)
        b_tip = _tip_via_rest(4043)
        assert caught, "B did NOT catch up over REST: B=%s A=%s\n%s" % (b_tip, a_tip, _tail(os.path.join(dir_b, "node.log"), 40))

        # PROOF: the two independently-built ledgers are byte-identical block-for-block
        ha = _block_hashes(os.path.join(dir_a, "regmode.db"))
        hb = _block_hashes(os.path.join(dir_b, "regmode.db"))
        for h in range(1, a_tip + 1):
            assert ha.get(h) == hb.get(h), "block %d hash mismatch: A=%s B=%s" % (h, ha.get(h), hb.get(h))
        assert hb.get(a_tip) is not None

        # prove it was the REST consume loop that did it (regnet has no socket sync, but be explicit)
        blog = _tail(os.path.join(dir_b, "node.log"), 300)
        assert "api_sync: ingested" in blog and "API-sync consume loop started" in blog, \
            "B caught up but its log doesn't show api_sync ingestion:\n" + blog
    finally:
        for proc, log, _ in procs:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            log.close()
