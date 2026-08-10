"""
Fork-transition smoke gate (standalone, like regnet_smoke.py — NOT collected by pytest).

Proves the hf2 transition is SMOOTH end to end on a live regnet node, across the events a real
deployment will see. One fork (hf2) bundles everything incl. the blake2b PoW, so the journey is:

  P1  fresh chain -> signal -> LOCK-IN (activation height H known, chain still pre-fork)
  P2  RESTART mid-transition (sidecar present) -> same H replays, tip preserved
  P3  real miner crosses the BOUNDARY: sha224 blocks to H-1, blake2b from H, no stall;
      a tx submitted PRE-activation confirms in a POST-activation block (mempool rides across)
  P4  REORG back across the boundary (rollback below H) and re-mine through it again
  P5  RESTART with the SIDECAR DELETED -> the digester re-derives the SAME H from the chain alone
      (before judging the next block — the self-healing ordering), re-persists it, chain continues
  P6  no 'fork-height detection failed' anywhere; final sidecar == {"hf2": H}

Regnet fork params (tests/config_custom.txt): window=5, boundary=10, bury=2 -> H is expected at 10.

NOTE on P5: block PRODUCERS read the cached node.fork_height, which is None for the first
generate after a sidecar-less restart, so the first mined block may be built in the wrong era and
(on mainnet difficulty) rejected — that single wasted block is expected; the digester's catch-up
runs BEFORE judging it, so nothing wrong-era can be ACCEPTED at real difficulty and the very next
block is era-correct. (On regnet's trivial difficulty a wrong-era block can pass by luck, which is
a property of regnet difficulty, not of the gate.)

Run from the repo root with the pytest session node NOT running:
    python3 tests/fork_transition_smoke.py
Exits 0 on success, 1 on the first failed phase.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PORT = 3030
API = "http://127.0.0.1:3031"
REGNET_PARAM = "regnet2"
SIDECAR = os.path.join(ROOT, "static", "fork_lockin-regmode.db.json")
LOGFILE = os.path.join(ROOT, "node_transition_smoke.log")

_proc = None
_log_fh = None


def say(msg):
    print(msg, flush=True)


def fail(msg):
    say("FAIL: " + msg)
    stop_node()
    sys.exit(1)


def _port_open():
    s = socket.socket()
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


def start_node(fresh=False):
    global _proc, _log_fh
    if fresh:
        for name in ("static/regmode.db", "static/index_reg.db",
                     "static/regmode.db-shm", "static/regmode.db-wal"):
            try:
                os.remove(os.path.join(ROOT, name))
            except OSError:
                pass
        for d in ("static/blockstore-regmode.db", "static/balanceindex-regmode.db",
                  "static/tokenindex-regmode.db"):
            shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)
        try:
            os.remove(SIDECAR)
        except OSError:
            pass
        shutil.copy(os.path.join(ROOT, "tests/config_custom.txt"),
                    os.path.join(ROOT, "config_custom.txt"))
        try:
            os.remove(LOGFILE)
        except OSError:
            pass
    _log_fh = open(LOGFILE, "a")
    env = dict(os.environ)
    if fresh:
        env.pop("BISMUTH_REGNET_KEEP", None)
    else:
        env["BISMUTH_REGNET_KEEP"] = "1"     # keep the regnet chain across this restart (test escape)
    _proc = subprocess.Popen([sys.executable, "node.py", REGNET_PARAM],
                             cwd=ROOT, stdout=_log_fh, stderr=subprocess.STDOUT, env=env)
    deadline = time.time() + 90
    while time.time() < deadline:
        if _port_open():
            return
        if _proc.poll() is not None:
            fail("node exited during startup; see %s" % LOGFILE)
        time.sleep(0.5)
    fail("node did not open port %d in time" % PORT)


def stop_node():
    global _proc, _log_fh
    if _proc is not None:
        _proc.terminate()
        try:
            _proc.wait(timeout=15)
        except Exception:
            _proc.kill()
        _proc = None
    if _log_fh is not None:
        _log_fh.close()
        _log_fh = None
    # the node must actually release the port before a restart
    deadline = time.time() + 10
    while time.time() < deadline and _port_open():
        time.sleep(0.3)


def api(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def read_log():
    try:
        with open(LOGFILE) as fh:
            return fh.read()
    except OSError:
        return ""


def main():
    if _port_open():
        fail("port %d already in use — stop the pytest session node first" % PORT)

    from _lite_client import LiteClient

    # ---- P1: fresh chain, signal to lock-in --------------------------------------------------
    say("P1: fresh boot, mining one block at a time to lock-in ...")
    start_node(fresh=True)
    client = LiteClient(os.path.join(ROOT, "wallet.der"), port=PORT)
    H = None
    for _ in range(20):
        client.mine(1)
        st = api("/api/fork")
        if st.get("locked_in"):
            H = st["fork_height"]
            break
    if H is None:
        fail("fork never locked in while signalling (status=%r)" % (api("/api/fork"),))
    # the digester's catch-up (and the sidecar write) runs at the TOP of the NEXT block's digest —
    # it derives from confirmed history only, so trigger one more digest before asserting persistence
    client.mine(1)
    tip = client.block_height()
    if not (tip < H):
        fail("lock-in detected too late: tip %s already >= H %s" % (tip, H))
    if not os.path.exists(SIDECAR):
        fail("lock-in did not persist the sidecar")
    say("P1 OK: locked in at tip %d, activation height H=%d, sidecar persisted" % (tip, H))

    # ---- P2: restart mid-transition (sidecar present) ----------------------------------------
    say("P2: restart between lock-in and activation ...")
    stop_node()
    start_node()
    client = LiteClient(os.path.join(ROOT, "wallet.der"), port=PORT)
    if client.block_height() != tip:
        fail("tip changed across restart: %s -> %s" % (tip, client.block_height()))
    st = api("/api/fork")
    if st.get("fork_height") != H:
        fail("activation height did not replay across restart: %r" % st)
    # the CACHE must be warm straight from the sidecar (node.py startup load), before any digest
    if api("/api/vm/contracts").get("fork_height") != H:
        fail("node.fork_height cache not loaded from the sidecar at startup")
    say("P2 OK: H=%d replayed from the sidecar (cache warm at boot), tip preserved at %d" % (H, tip))

    # ---- P3: real miner across the boundary; pre-fork tx confirms post-fork -------------------
    say("P3: real miner (miner.py) mines up to H-1, tx submitted pre-fork, then across ...")
    while client.block_height() < H - 1:
        before = client.block_height()
        client.mine_real(1)
        if client.block_height() != before + 1:
            fail("real miner failed to extend pre-fork chain at tip %d" % before)
    recipient = "0" * 56
    base = float(client.balance(recipient) or 0)
    client.send(recipient, 1.0)                       # submitted at tip H-1: PRE-activation
    confirmed = False
    for _ in range(4):                                # poll-for-inclusion (regnet mempool gotcha)
        client.mine_real(1)
        if float(client.balance(recipient) or 0) >= base + 1.0:
            confirmed = True
            break
    if not confirmed:
        fail("pre-fork tx did not confirm in a post-fork block")
    tip = client.block_height()
    if tip < H:
        fail("chain failed to cross the activation boundary (tip %d < H %d)" % (tip, H))
    st = api("/api/fork")
    if not st.get("active"):
        fail("/api/fork not active past H: %r" % st)
    if api("/api/fee").get("post_fork") is not True:
        fail("dynamic fee not live past H")
    say("P3 OK: boundary crossed smoothly (tip %d), pre-fork tx confirmed post-fork, fees live" % tip)

    # ---- P4: reorg back across the boundary, re-mine through it again -------------------------
    say("P4: rollback below H and re-mine across the boundary ...")
    client.rollback(H)                                # drops every post-fork block; tip -> H-1
    tip = client.block_height()
    if tip != H - 1:
        fail("rollback landed at %s, expected %s" % (tip, H - 1))
    for _ in range(3):
        client.mine_real(1)
    tip = client.block_height()
    if tip < H + 1:
        fail("could not re-cross the boundary after the reorg (tip %d)" % tip)
    if not api("/api/fork").get("active"):
        fail("fork not active after re-crossing")
    say("P4 OK: reorged to %d and re-crossed to %d without a stall" % (H - 1, tip))

    # ---- P5: restart with the sidecar DELETED — self-healing re-derivation --------------------
    say("P5: restart with the lock-in sidecar deleted ...")
    stop_node()
    os.remove(SIDECAR)
    log_before = read_log()
    start_node()
    client = LiteClient(os.path.join(ROOT, "wallet.der"), port=PORT)
    pre = api("/api/vm/contracts").get("fork_height")
    if pre is not None:
        fail("cache unexpectedly warm after sidecar-less restart: %r" % pre)
    tip0 = client.block_height()
    client.mine_real(1)                               # may be wasted (stale era pre-first-digest)
    client.mine_real(2)                               # era-correct: catch-up ran during first digest
    if client.block_height() < tip0 + 2:
        fail("chain did not advance after sidecar-less restart")
    if api("/api/vm/contracts").get("fork_height") != H:
        fail("digester did not re-derive H=%d from the chain alone" % H)
    with open(SIDECAR) as fh:
        sidecar = json.load(fh)
    if sidecar != {"hf2": H}:
        fail("re-persisted sidecar wrong: %r" % sidecar)
    relock = read_log()[len(log_before):]
    if "activation height locked at %d" % H not in relock:
        fail("no fresh lock-in log line after sidecar-less restart")
    say("P5 OK: H=%d re-derived from chain, sidecar re-persisted, chain continues" % H)

    # ---- P6: hygiene -------------------------------------------------------------------------
    log = read_log()
    if "fork-height detection failed" in log:
        fail("fork-height detection errored somewhere — see %s" % LOGFILE)
    stop_node()
    say("P6 OK: no detection errors in any phase")
    say("ALL PHASES PASSED — transition is smooth (H=%d, one fork, one height)" % H)


if __name__ == "__main__":
    try:
        main()
    finally:
        stop_node()
