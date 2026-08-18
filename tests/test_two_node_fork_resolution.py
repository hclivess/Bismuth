"""
Two-node regnet harness for doc/47 fork resolution — a REAL fork over the REAL socket protocol.

Two independent regnet node processes (isolated data dirs, ports 4070-4073 — NEVER prod / 5658-5659)
each mine their own chain from the shared genesis, then get peered (BISMUTH_REGNET_PEERING=1 lets a regnet
node dial and answer "hello"; the peer file is written by the test once the fork exists). Assertions:

  1. LONGER CHAIN WINS: the shorter node (B) measures the ancestor (genesis), FETCHES A's branch first, and
     rolls back ONCE to the ancestor — its log shows a single "rolling back N block(s) to the measured
     ancestor" and "adopted the peer's branch"; both nodes end on the same tip hash. A never rolls back.
  2. SAME-HEIGHT RACE: both mine one block concurrently while connected. Whatever the timing produced, the
     nodes converge on one tip hash, and NO node rolls back more than once (no seesaw); if a genuine tie was
     produced (both logs show a same-height verdict) exactly one side reorged.

Heavy (two node processes); gated:
    BISMUTH_RUN_TWONODE=1 python3 -m pytest tests/test_two_node_fork_resolution.py -v -s
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

A_SOCK, A_REST, B_SOCK, B_REST = 4070, 4071, 4072, 4073


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _start_node(datadir, sock_port, rest_port, extra_env):
    os.makedirs(datadir, exist_ok=True)
    env = os.environ.copy()
    env["BISMUTH_REGNET_PORT"] = str(sock_port)
    env["BISMUTH_REST_API_PORT"] = str(rest_port)
    env["BISMUTH_REGNET_DB"] = os.path.join(datadir, "regmode.db")
    env["BISMUTH_REGNET_INDEX"] = os.path.join(datadir, "index_reg.db")
    env["BISMUTH_REGNET_PEERS"] = os.path.join(datadir, "peers_reg.txt")
    env["BISMUTH_MEMPOOL_PATH"] = os.path.join(datadir, "mempool.db")
    env["BISMUTH_REGNET_PEERING"] = "1"
    env.update(extra_env)
    log = open(os.path.join(datadir, "node.log"), "w")
    proc = subprocess.Popen([sys.executable, "node.py", "regnet2"], cwd=ROOT,
                            stdout=log, stderr=subprocess.STDOUT, env=env)
    return proc, log


def _await(cond, deadline_s, poll=0.5):
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


def _log_text(datadir):
    try:
        with open(os.path.join(datadir, "node.log")) as f:
            return f.read()
    except Exception:
        return ""


def _tail(datadir, n=40):
    return "".join(_log_text(datadir).splitlines(True)[-n:])


def _tip(client):
    r = client.command("blocklastjson")
    return int(r["block_height"]), str(r["block_hash"])


def _stop(procs):
    for p, log, d in procs:
        try:
            p.terminate()
            p.wait(timeout=15)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        try:
            log.close()
        except Exception:
            pass


@pytest.mark.skipif(not os.environ.get("BISMUTH_RUN_TWONODE"),
                    reason="heavy two-node harness; run explicitly with BISMUTH_RUN_TWONODE=1")
def test_two_node_fork_resolution(tmp_path):
    if not os.path.exists(os.path.join(ROOT, "config_custom.txt")):
        shutil.copy(os.path.join(ROOT, "tests/config_custom.txt"), os.path.join(ROOT, "config_custom.txt"))
    from _lite_client import LiteClient
    dir_a, dir_b = str(tmp_path / "a"), str(tmp_path / "b")
    procs = []
    try:
        pa, la = _start_node(dir_a, A_SOCK, A_REST, {})
        procs.append((pa, la, dir_a))
        pb, lb = _start_node(dir_b, B_SOCK, B_REST, {})
        procs.append((pb, lb, dir_b))
        assert _await(lambda: _port_open(A_SOCK), 120), "A never opened\n" + _tail(dir_a)
        assert _await(lambda: _port_open(B_SOCK), 120), "B never opened\n" + _tail(dir_b)
        wallet = os.path.join(ROOT, "wallet.der")
        ca = LiteClient(wallet, port=A_SOCK)
        cb = LiteClient(wallet, port=B_SOCK)
        assert _await(lambda: ca.block_height() >= 1 and cb.block_height() >= 1, 60)

        # ---- build a real fork: A mines 6, B mines 3, from the shared genesis, while unpeered ----
        ca.mine(6)
        cb.mine(3)
        a_h, a_hash = _tip(ca)
        b_h, b_hash = _tip(cb)
        assert a_h == 7 and b_h == 4 and a_hash != b_hash, (a_h, b_h)
        # sanity: still unpeered — B's height did not move
        time.sleep(2)
        assert _tip(cb)[0] == 4

        # ---- peer them (each dials the other; the client_loop reloads the peer file every 5 s) ----
        for d, other in ((dir_a, B_SOCK), (dir_b, A_SOCK)):
            with open(os.path.join(d, "peers_reg.txt"), "w") as f:
                json.dump({"127.0.0.1": str(other)}, f)

        # ---- 1. longer chain wins: B adopts A's branch, once ----
        ok = _await(lambda: _tip(cb) == (a_h, a_hash), 150)
        assert ok, ("B never converged on A's tip: B=%s A=%s\n--- B log ---\n%s\n--- A log ---\n%s"
                    % (_tip(cb), _tip(ca), _tail(dir_b, 60), _tail(dir_a, 40)))
        assert _tip(ca) == (a_h, a_hash), "A (the longer chain) must not have moved"
        blog = _log_text(dir_b)
        assert "adopted the peer's branch" in blog, _tail(dir_b, 60)
        assert blog.count("to the measured ancestor") == 1, "B must roll back exactly ONCE\n" + _tail(dir_b, 80)
        assert "rolling back 3 block(s) to the measured ancestor 1" in blog, _tail(dir_b, 80)
        assert "to the measured ancestor" not in _log_text(dir_a), "A must never roll back\n" + _tail(dir_a, 60)
        b_rollbacks_after_1 = _log_text(dir_b).count("to the measured ancestor")
        a_rollbacks_after_1 = _log_text(dir_a).count("to the measured ancestor")

        # ---- 2. same-height race while connected ----
        h0 = _tip(ca)[0]
        assert _tip(cb)[0] == h0
        errs = []

        def m(c):
            try:
                c.mine(1)
            except Exception as e:
                errs.append(e)
        ta, tb = threading.Thread(target=m, args=(ca,)), threading.Thread(target=m, args=(cb,))
        ta.start(); tb.start(); ta.join(); tb.join()
        assert not errs, errs
        # converge: same tip hash on both, at h0+1 or h0+2 depending on whether the race produced a tie
        ok = _await(lambda: _tip(ca) == _tip(cb) and _tip(ca)[0] >= h0 + 1, 150)
        assert ok, ("no convergence after the race: A=%s B=%s\n--- A ---\n%s\n--- B ---\n%s"
                    % (_tip(ca), _tip(cb), _tail(dir_a, 60), _tail(dir_b, 60)))
        alog, blog = _log_text(dir_a), _log_text(dir_b)
        a_rb = alog.count("to the measured ancestor") - a_rollbacks_after_1
        b_rb = blog.count("to the measured ancestor") - b_rollbacks_after_1
        assert a_rb <= 1 and b_rb <= 1, "seesaw: A rolled %d, B rolled %d" % (a_rb, b_rb)
        tie_seen = ("same-height fork" in alog) or ("same-height fork" in blog)
        print("\n[two-node] race outcome: tip=%s tie_seen=%s A_rollbacks=%d B_rollbacks=%d"
              % (_tip(ca), tie_seen, a_rb, b_rb))
        if tie_seen:
            assert a_rb + b_rb == 1, "a genuine tie must make exactly one side reorg"
            assert ("wins the tie-break" in alog) or ("wins the tie-break" in blog)
    finally:
        _stop(procs)
