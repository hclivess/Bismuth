"""
Live sequence/dupe self-heal (chain_ops.detect_recent_sequence_break / autoheal_live).

Two layers:
  * HERMETIC — the recent-tail detector against a synthetic coinbase sequence: clean -> None, a duplicate
    height -> that height, a gap -> the missing height. No node needed.
  * LIVE (env-gated BISMUTH_RUN_TWONODE) — a real regnet node with a short autoheal interval: mine a chain,
    inject a duplicate coinbase at the tip directly into its ledger, and confirm the node rolls the chain
    back past the corruption ON ITS OWN (no restart), then mines cleanly on top again.

Run hermetic: python3 -m pytest tests/test_autoheal.py -v
Run live too: BISMUTH_RUN_TWONODE=1 python3 -m pytest tests/test_autoheal.py -v -s
"""
import os
import sqlite3
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import chain_ops


class _FakeDB:
    """Minimal db_handler shim: chain_ops.detect_recent_sequence_break only needs `.h` + `.fetchall`."""
    def __init__(self, conn):
        self.h = conn.cursor()

    def fetchall(self, cursor, sql, params=()):
        cursor.execute(sql, params)
        return cursor.fetchall()


def _ledger_with_coinbases(path, heights):
    """A transactions table with one reward!=0 coinbase per height in `heights` (in insertion order)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, amount, "
                 "signature, public_key, block_hash, fee, reward, operation, openfield)")
    for h in heights:
        conn.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (h, 1600000000 + h, "miner", "miner", 0, "sig%d" % h, "pk", "h%d" % h, 0, 10, "", ""))
    conn.commit()
    return conn


def test_detector_clean(tmp_path):
    conn = _ledger_with_coinbases(str(tmp_path / "l.db"), range(2, 41))   # 2..40 consecutive
    assert chain_ops.detect_recent_sequence_break(_FakeDB(conn), tip=40, window=2000) is None
    conn.close()


def test_detector_duplicate_height(tmp_path):
    conn = _ledger_with_coinbases(str(tmp_path / "l.db"), range(2, 41))
    # inject a SECOND coinbase at height 30 -> a dupe block
    conn.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (30, 1600000030, "miner", "miner", 0, "dup", "pk", "h30b", 0, 10, "", ""))
    conn.commit()
    assert chain_ops.detect_recent_sequence_break(_FakeDB(conn), tip=40, window=2000) == 30
    conn.close()


def test_detector_gap(tmp_path):
    # 2..29 then 31..40 (height 30 missing) -> break at the missing height 30
    conn = _ledger_with_coinbases(str(tmp_path / "l.db"), list(range(2, 30)) + list(range(31, 41)))
    assert chain_ops.detect_recent_sequence_break(_FakeDB(conn), tip=40, window=2000) == 30
    conn.close()


def test_detector_only_scans_recent_window(tmp_path):
    # a dupe far below the window must NOT be flagged by the live (recent-tail) check
    conn = _ledger_with_coinbases(str(tmp_path / "l.db"), range(2, 5001))
    conn.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (100, 1600000100, "miner", "miner", 0, "dup", "pk", "h100b", 0, 10, "", ""))
    conn.commit()
    assert chain_ops.detect_recent_sequence_break(_FakeDB(conn), tip=5000, window=2000) is None  # 100 < 5000-2000
    conn.close()


# ---------------------------------------------------------------- live end-to-end (env-gated) ----
import json
import socket
import subprocess
import urllib.request


def _rest(port, path, timeout=8):
    with urllib.request.urlopen("http://127.0.0.1:%d/api/%s" % (port, path), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _tip(port):
    try:
        return int(_rest(port, "status").get("blocks", 0) or 0)
    except Exception:
        return -1


@pytest.mark.skipif(not os.environ.get("BISMUTH_RUN_TWONODE"),
                    reason="heavy live node test; run with BISMUTH_RUN_TWONODE=1")
def test_autoheal_live_recovers_from_injected_dupe(tmp_path):
    if not os.path.exists(os.path.join(ROOT, "config_custom.txt")):
        import shutil
        shutil.copy(os.path.join(ROOT, "tests/config_custom.txt"), os.path.join(ROOT, "config_custom.txt"))
    datadir = str(tmp_path / "n")
    os.makedirs(datadir, exist_ok=True)
    ledger = os.path.join(datadir, "regmode.db")
    env = os.environ.copy()
    env.update({"BISMUTH_REGNET_PORT": "4044", "BISMUTH_REST_API_PORT": "4045",
                "BISMUTH_REGNET_DB": ledger, "BISMUTH_REGNET_INDEX": os.path.join(datadir, "index_reg.db"),
                "BISMUTH_REGNET_PEERS": os.path.join(datadir, "peers_reg.txt"),
                "BISMUTH_MEMPOOL_PATH": os.path.join(datadir, "mempool.db"),
                "BISMUTH_AUTOHEAL_INTERVAL": "4"})
    log = open(os.path.join(datadir, "node.log"), "w")
    proc = subprocess.Popen([sys.executable, "node.py", "regnet2"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=env)
    try:
        end = time.time() + 90
        while time.time() < end and _tip(4045) < 1:
            time.sleep(1)
        assert _tip(4045) >= 1, "node never came up"

        from _lite_client import LiteClient
        ca = LiteClient(os.path.join(ROOT, "wallet.der"), port=4044)
        ca.mine(20)
        tip = _tip(4045)
        assert tip >= 18, "only reached %s" % tip

        # inject a duplicate coinbase at a recent height directly into the running node's ledger
        dup_h = tip - 2
        conn = sqlite3.connect(ledger, timeout=20)
        try:
            row = conn.execute("SELECT * FROM transactions WHERE block_height=? AND reward!=0 LIMIT 1",
                               (dup_h,)).fetchone()
            assert row is not None, "no coinbase at %s to duplicate" % dup_h
            conn.execute("INSERT INTO transactions VALUES (%s)" % ",".join("?" * len(row)), row)
            conn.commit()
        finally:
            conn.close()

        # autoheal (interval 4s) should detect the dupe and roll back below it WITHOUT a restart
        end = time.time() + 60
        healed = False
        while time.time() < end:
            if _tip(4045) < dup_h:
                healed = True
                break
            time.sleep(2)
        assert healed, "autoheal did not roll back the injected dupe (tip still %s, dup at %s)" % (_tip(4045), dup_h)

        # and the node is healthy afterward: it mines cleanly on the repaired chain
        before = _tip(4045)
        ca.mine(3)
        end = time.time() + 30
        while time.time() < end and _tip(4045) <= before:
            time.sleep(1)
        assert _tip(4045) > before, "node did not mine after autoheal (stuck at %s)" % _tip(4045)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()
