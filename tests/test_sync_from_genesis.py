"""doc/30 — from-genesis sync: genesis seeding + checkpoint-anchored trusted prefix.

Two layers:
  * HERMETIC — chain_ops.seed_genesis creates a canonical genesis-only ledger and the
    bootstrap() guard seeds instead of downloading when sync_from_genesis is set. No node.
  * END-TO-END (env-gated BISMUTH_RUN_TWONODE) — two real regnet nodes. A mines a chain past
    a checkpoint height; B starts at GENESIS and catches A's chain over REST with a checkpoint
    + assume_valid_height set, so it skips per-item validation below the horizon and must
    reproduce the canonical block hash at the checkpoint. Positive: B reaches A's tip and logs
    "checkpoint OK". Negative: a WRONG checkpoint hash halts B at the checkpoint height.

Run hermetic: python3 -m pytest tests/test_sync_from_genesis.py -v
Run E2E too:  BISMUTH_RUN_TWONODE=1 python3 -m pytest tests/test_sync_from_genesis.py -v -s
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

GENESIS_HASH = "7a0f384876aca3871adbde8622a87f8b971ede0ed8ee10425e3958a1"
GENESIS_ADDR = "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed"


@pytest.fixture(autouse=True)
def _pin_decimal_amounts():
    import amounts
    prev = amounts.LEDGER_INTEGER
    amounts.LEDGER_INTEGER = False
    yield
    amounts.LEDGER_INTEGER = prev


def _node(tmp):
    return types.SimpleNamespace(
        hyper_path=str(tmp / "hyper.db"), ledger_path=str(tmp / "ledger.db"),
        index_db=str(tmp / "index.db"), sync_from_genesis=True,
        logger=types.SimpleNamespace(app_log=types.SimpleNamespace(warning=lambda m: None)))


# ============================================================ HERMETIC ====
def test_seed_genesis_creates_canonical_genesis(tmp_path):
    import chain_ops
    n = _node(tmp_path)
    chain_ops.seed_genesis(n)
    for path in (n.ledger_path, n.hyper_path):
        con = sqlite3.connect(path)
        rows = con.execute("SELECT block_height, address, recipient, block_hash FROM transactions").fetchall()
        tip = con.execute("SELECT MAX(block_height) FROM transactions").fetchone()[0]
        con.close()
        assert tip == 1, "%s should hold only the genesis block" % path
        h, addr, rcpt, bh = rows[0]
        assert (h, addr, rcpt, bh) == (1, "genesis", GENESIS_ADDR, GENESIS_HASH)
    assert os.path.exists(n.index_db)


def test_seed_genesis_does_not_clobber_populated_ledger(tmp_path):
    import chain_ops
    n = _node(tmp_path)
    con = sqlite3.connect(n.ledger_path)
    con.execute("CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, amount, "
                "signature, public_key, block_hash, fee, reward, operation, openfield)")
    con.execute("INSERT INTO transactions (block_height, block_hash) VALUES (500, 'existing')")
    con.commit(); con.close()
    chain_ops.seed_genesis(n)                                   # must NOT wipe the populated ledger
    con = sqlite3.connect(n.ledger_path)
    tip = con.execute("SELECT MAX(block_height) FROM transactions").fetchone()[0]
    con.close()
    assert tip == 500, "seed_genesis clobbered a populated ledger"


def test_bootstrap_guard_seeds_instead_of_downloading(tmp_path, monkeypatch):
    """With sync_from_genesis set, bootstrap() seeds genesis and never downloads."""
    import chain_ops
    called = {"download": 0}
    monkeypatch.setattr(chain_ops, "download_file",
                        lambda *a, **k: called.__setitem__("download", called["download"] + 1))
    n = _node(tmp_path); n.bootstrap_file = ""; n.bootstrap_url = "http://example/ledger.tar.gz"
    chain_ops.bootstrap(n)
    assert called["download"] == 0, "bootstrap downloaded a snapshot despite sync_from_genesis"
    con = sqlite3.connect(n.ledger_path)
    bh = con.execute("SELECT block_hash FROM transactions WHERE block_height=1").fetchone()[0]
    con.close()
    assert bh == GENESIS_HASH


# ============================================================ END-TO-END (regnet) ====
def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _tip(port):
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port, timeout=8) as r:
            return int(json.loads(r.read().decode()).get("blocks", 0) or 0)
    except Exception:
        return -1


def _start(datadir, sock_port, rest_port, extra_env):
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


def _block_hash_at(ledger_path, height):
    conn = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=20)
    conn.text_factory = str
    try:
        row = conn.execute("SELECT block_hash FROM transactions WHERE block_height=? LIMIT 1",
                           (height,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _tail(path, n=40):
    try:
        with open(path) as f:
            return "".join(f.readlines()[-n:])
    except Exception:
        return "(no log)"


@pytest.mark.skipif(not os.environ.get("BISMUTH_RUN_TWONODE"),
                    reason="heavy two-node harness; run explicitly with BISMUTH_RUN_TWONODE=1")
def test_from_genesis_checkpoint_sync(tmp_path):
    """B and C both start at genesis and catch A's chain over REST. B has the CORRECT checkpoint
    (reaches A's tip, logs 'checkpoint OK'); C has a WRONG checkpoint (halts at the checkpoint)."""
    if not os.path.exists(os.path.join(ROOT, "config_custom.txt")):
        shutil.copy(os.path.join(ROOT, "tests/config_custom.txt"), os.path.join(ROOT, "config_custom.txt"))
    H_CP = 8                                                    # pre-fork checkpoint height
    dir_a, dir_b, dir_c = str(tmp_path / "a"), str(tmp_path / "b"), str(tmp_path / "c")
    procs = []
    try:
        # --- A: mine a chain well past the checkpoint ---
        pa, la = _start(dir_a, 4060, 4061, {})
        procs.append((pa, la))
        assert _await(lambda: _port_open(4060), 90), "A socket never opened\n" + _tail(os.path.join(dir_a, "node.log"))
        assert _await(lambda: _tip(4061) >= 1, 90), "A REST never came up\n" + _tail(os.path.join(dir_a, "node.log"))
        from _lite_client import LiteClient
        LiteClient(os.path.join(ROOT, "wallet.der"), port=4060).mine(16)
        a_tip = _tip(4061)
        assert a_tip >= H_CP + 4, "A only reached %s" % a_tip
        cp_hash = _block_hash_at(os.path.join(dir_a, "regmode.db"), H_CP)
        assert cp_hash, "no block hash at checkpoint height %d" % H_CP

        common = {"BISMUTH_API_SYNC": "1", "BISMUTH_API_SYNC_SOURCE": "127.0.0.1:4061",
                  "BISMUTH_ASSUME_VALID_HEIGHT": str(H_CP)}

        # --- B: CORRECT checkpoint -> trusts prefix below H_CP, anchors at H_CP, reaches A's tip ---
        pb, lb = _start(dir_b, 4062, 4063,
                        dict(common, BISMUTH_CHECKPOINTS=json.dumps({str(H_CP): cp_hash})))
        procs.append((pb, lb))
        assert _await(lambda: _tip(4063) >= 1, 90), "B REST never came up\n" + _tail(os.path.join(dir_b, "node.log"))
        caught = _await(lambda: _tip(4063) >= a_tip, 150, poll=2.0)
        blog = _tail(os.path.join(dir_b, "node.log"), 400)
        assert caught, "B did not reach A's tip (B=%s A=%s) from genesis with a checkpoint\n%s" % (
            _tip(4063), a_tip, blog)
        assert "checkpoint OK at height %d" % H_CP in blog, \
            "B reached tip but never logged the checkpoint verification\n" + blog

        # --- C: WRONG checkpoint -> must HALT at H_CP and never reach A's tip ---
        bad = "0" * len(cp_hash)
        pc, lc = _start(dir_c, 4064, 4065,
                        dict(common, BISMUTH_CHECKPOINTS=json.dumps({str(H_CP): bad})))
        procs.append((pc, lc))
        assert _await(lambda: _tip(4065) >= 1, 90), "C REST never came up\n" + _tail(os.path.join(dir_c, "node.log"))
        # give C ample time to TRY to cross the checkpoint
        stuck = not _await(lambda: _tip(4065) >= H_CP + 1, 60, poll=2.0)
        c_tip = _tip(4065)
        clog = _tail(os.path.join(dir_c, "node.log"), 400)
        assert stuck and c_tip < H_CP + 1, \
            "C crossed a WRONG checkpoint (tip %s) — the anchor failed to halt it\n%s" % (c_tip, clog)
        assert "checkpoint MISMATCH at height %d" % H_CP in clog, \
            "C stalled but not for the checkpoint mismatch reason\n" + clog
    finally:
        for proc, log in procs:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            log.close()
