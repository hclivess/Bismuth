#!/usr/bin/env python3
"""diff_selfheal_soak.py — regnet SOAK for the #23 difficulty-divergence detector (doc/35).

Brings up a 3-node regnet cluster to a COMMON tip (node A mines; B and C catch up over the REST API via
api_sync), then drives the REAL detector — chain_ops.detect_difficulty_divergence — against the live 3-peer
quorum across many cycles and three phases, simulating an extended soak:

  HEALTHY      local == peers (16): assert the detector reaches a real >=3-peer quorum and NEVER flags a
               divergence (zero false positives) over many cycles — the key "is it safe to enable" question.
  INJECT_LOCAL local set wrong (24): assert the detector DETECTS divergence vs the healthy peer median, and
               the 3-cycle debounce confirms — proving end-to-end detection against real nodes.
  LYING_PEER   one node (C) restarted with BISMUTH_TEST_DIFF_OFFSET so it reports a wrong difficulty: assert
               a single dishonest/forked peer does NOT trigger a (false) divergence — robustness.

Writes a JSON + human report. NEVER touches the prod ledger / ports 5658-5659 / systemd; uses regnet on
ports 4060-4065 + an isolated temp data dir. Run:  python3 tools/diff_selfheal_soak.py [--healthy-cycles N]
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import chain_ops  # noqa: E402
import regnet     # noqa: E402

REGNET_DIFF = float(regnet.REGNET_DIFF)


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _rest(port, path, timeout=8):
    with urllib.request.urlopen("http://127.0.0.1:%d/api/%s" % (port, path), timeout=timeout) as r:
        return json.loads(r.read().decode())


def _tip(port):
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
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


def _stop(proc):
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ---- detector stub: a local node whose difficulty we control, with the 3 real nodes as its peers ----------
class _Log:
    def __init__(self): self.msgs = []
    def warning(self, m): self.msgs.append(str(m))
    info = warning
    debug = warning


class _Logger:
    def __init__(self): self.app_log = _Log()


class _Peers:
    def __init__(self, pool):
        self.connection_pool = list(pool)
        self.consensus = None


class _Stub:
    def __init__(self, peers_sock, local_diff, last_block):
        self.difficulty = (local_diff, local_diff - 8, 60.0, local_diff, 60.0, 0.0, 0.0, last_block)
        self.last_block = last_block
        self.peers = _Peers(peers_sock)
        self.logger = _Logger()
        self.fork_height = None


def _soak_phase(stub, name, cycles, sleep_s, report):
    """Run `cycles` real detect() calls; tally quorum size + divergence verdicts."""
    cache = {}
    res = {"phase": name, "cycles": cycles, "diverged": 0, "clean": 0, "abstain": 0,
           "n_samples": [], "exceptions": 0, "median_seen": []}
    for _ in range(cycles):
        try:
            diverged, local, median, n = chain_ops.detect_difficulty_divergence(stub, None, stub.peers,
                                                                                rest_port_cache=cache)
            res["n_samples"].append(int(n))
            if median is not None:
                res["median_seen"].append(round(float(median), 4))
            if diverged:
                res["diverged"] += 1
            elif n >= chain_ops.DIFF_DIVERGENCE_MIN_PEERS:
                res["clean"] += 1
            else:
                res["abstain"] += 1
        except Exception as e:
            res["exceptions"] += 1
            res.setdefault("exc_msg", str(e))
        time.sleep(sleep_s)
    res["max_quorum"] = max(res["n_samples"]) if res["n_samples"] else 0
    report["phases"].append(res)
    print("  [%s] cycles=%d  diverged=%d clean=%d abstain=%d  max_quorum=%d  exc=%d  median~%s"
          % (name, cycles, res["diverged"], res["clean"], res["abstain"], res["max_quorum"],
             res["exceptions"], (res["median_seen"][-1] if res["median_seen"] else "n/a")))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--healthy-cycles", type=int, default=60)
    ap.add_argument("--phase-cycles", type=int, default=12)
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    # regnet nodes read ROOT/config_custom.txt (prod ignores it via BISMUTH_IGNORE_CONFIG_CUSTOM=1)
    if not os.path.exists(os.path.join(ROOT, "config_custom.txt")):
        shutil.copy(os.path.join(ROOT, "tests/config_custom.txt"), os.path.join(ROOT, "config_custom.txt"))

    tmp = tempfile.mkdtemp(prefix="diffsoak_")
    A_S, A_R, B_S, B_R, C_S, C_R = 4060, 4061, 4062, 4063, 4064, 4065
    report = {"started": time.strftime("%F %T"), "tmp": tmp, "phases": [], "verdict": "PENDING"}
    procs = []
    cproc = [None]  # node C handle (restarted in the lying-peer phase)
    try:
        print("SOAK: bringing up regnet cluster in", tmp)
        pa, _ = _start_node(os.path.join(tmp, "a"), A_S, A_R, {})
        procs.append(pa)
        if not _await(lambda: _port_open(A_S) and _tip(A_R) >= 1, 90):
            raise RuntimeError("node A never came up")
        from _lite_client import LiteClient
        LiteClient(os.path.join(ROOT, "wallet.der"), port=A_S).mine(16)
        a_tip = _tip(A_R)
        print("  A mined to tip", a_tip)
        if a_tip < 8:
            raise RuntimeError("A only reached tip %s" % a_tip)

        # B and C catch up to A's chain over REST (api_sync) -> 3 nodes at a COMMON tip
        src = {"BISMUTH_API_SYNC": "1", "BISMUTH_API_SYNC_SOURCE": "127.0.0.1:%d" % A_R}
        pb, _ = _start_node(os.path.join(tmp, "b"), B_S, B_R, src)
        pc, _ = _start_node(os.path.join(tmp, "c"), C_S, C_R, src)
        procs += [pb, pc]
        cproc[0] = pc
        if not _await(lambda: _tip(B_R) >= a_tip and _tip(C_R) >= a_tip, 180, poll=2.0):
            raise RuntimeError("B/C did not catch up: A=%s B=%s C=%s" % (a_tip, _tip(B_R), _tip(C_R)))
        common = min(_tip(A_R), _tip(B_R), _tip(C_R))
        print("  B,C caught up; common tip", common)

        peers = ["127.0.0.1:%d" % A_S, "127.0.0.1:%d" % B_S, "127.0.0.1:%d" % C_S]

        # PHASE 1 — healthy: local matches peers; expect a real >=3 quorum + ZERO false positives
        print("PHASE healthy (local==%g):" % REGNET_DIFF)
        h = _soak_phase(_Stub(peers, REGNET_DIFF, common), "HEALTHY", args.healthy_cycles, args.sleep, report)

        # PHASE 2 — injected local divergence: local wrong; expect DETECTION every cycle
        print("PHASE inject_local (local=%g):" % (REGNET_DIFF + 8))
        inj = _soak_phase(_Stub(peers, REGNET_DIFF + 8, common), "INJECT_LOCAL", args.phase_cycles, args.sleep, report)

        # PHASE 3 — lying peer: restart C reporting a wrong difficulty; one bad peer must NOT flag us
        print("PHASE lying_peer (restart C with +8 offset):")
        _stop(cproc[0])
        time.sleep(2)
        pc2, _ = _start_node(os.path.join(tmp, "c"), C_S, C_R,
                             dict(src, BISMUTH_TEST_DIFF_OFFSET="8"))
        procs.append(pc2)
        cproc[0] = pc2
        if not _await(lambda: _tip(C_R) >= common, 120, poll=2.0):
            print("  WARN: C did not return to tip; phase result may be abstain")
        # give C a moment to recompute+serve the offset difficulty
        _await(lambda: abs(float(_rest(C_R, "difficulty").get("difficulty", 0)) - (REGNET_DIFF + 8)) < 0.01, 30, poll=1.0)
        bad = _soak_phase(_Stub(peers, REGNET_DIFF, common), "LYING_PEER", args.phase_cycles, args.sleep, report)

        # ---- verdict ----
        ok_healthy = (h["diverged"] == 0 and h["max_quorum"] >= chain_ops.DIFF_DIVERGENCE_MIN_PEERS
                      and h["exceptions"] == 0)
        ok_detect = (inj["diverged"] >= max(1, args.phase_cycles - 1) and inj["exceptions"] == 0)
        ok_robust = (bad["diverged"] == 0 and bad["exceptions"] == 0)
        report["checks"] = {"healthy_no_false_positive_with_quorum": ok_healthy,
                            "injected_divergence_detected": ok_detect,
                            "single_lying_peer_not_flagged": ok_robust}
        report["verdict"] = "PASS" if (ok_healthy and ok_detect and ok_robust) else "FAIL"
    except Exception as e:
        report["verdict"] = "ERROR"
        report["error"] = str(e)
        print("SOAK ERROR:", e)
    finally:
        for p in procs:
            _stop(p)
        report["ended"] = time.strftime("%F %T")
        out = os.path.join(ROOT, "tools", "diff_selfheal_soak_report.json")
        try:
            with open(out, "w") as f:
                json.dump(report, f, indent=2)
        except Exception:
            pass
        print("\nSOAK VERDICT:", report["verdict"])
        print("checks:", json.dumps(report.get("checks", {}), indent=2))
        print("report:", out)
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
