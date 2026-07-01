"""
Bridge artifacts on a 3-node regnet cluster (doc/45) — the cross-node consensus validation of everything
the bridge added: the VM crypto syscalls (SYS_KECCAK256 / SYS_ECRECOVER), the Merkle state commitment
(Stage 2b), and the peg-in vault. Mirrors tests/test_multinode_integration.py: node A mines a chain across
the regnet hf2 fork and deploys the bridge artifacts; B and C start EMPTY and reconstruct A's chain over
api_sync (REST only — regnet has no socket peering). Then, using ONLY the REST API, we assert that all three
INDEPENDENTLY-BUILT ledgers agree:

  1. CHAIN PARITY        — every block hash 1..tip byte-identical across A,B,C.
  2. COMMITTED ROOT      — /api/vm/contracts state_root (now the MERKLE root, Stage 2b) identical on A,B,C.
  3. VAULT (peg-in)      — bridge_vault custody balance + the lock record (storage) identical on A,B,C.
  4. CRYPTO SYSCALLS     — a probe contract that SSTOREs keccak256(payload) and ecrecover(payload,sig) has
                           IDENTICAL storage on A,B,C — i.e. the new syscalls executed byte-identically on
                           the consensus path across independent ledgers (a non-deterministic syscall would
                           have diverged the state root and broken parity #1).

Heavy: manages its own 3 node processes. Gated — run explicitly with:
    BISMUTH_RUN_MULTINODE=1 python3 -m pytest tests/test_bridge_multinode.py -v -s
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
import coincurve
from Cryptodome.Hash import keccak as _keccak

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "tests"), os.path.join(ROOT, "contracts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _kec(b):
    h = _keccak.new(digest_bits=256); h.update(b); return h.digest()


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _rest(port, path, timeout=8):
    with urllib.request.urlopen("http://127.0.0.1:%d/api/%s" % (port, path), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


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
    p = subprocess.Popen([sys.executable, "node.py", "regnet2"], cwd=ROOT, stdout=log,
                         stderr=subprocess.STDOUT, env=env)
    return p, log


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


def _tail(path, n=40):
    try:
        return "".join(open(path).readlines()[-n:])
    except Exception:
        return "(no log)"


def _block_hashes(ledger_path):
    conn = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=20)
    conn.text_factory = str
    try:
        rows = conn.execute("SELECT block_height, block_hash FROM transactions "
                            "WHERE block_height > 0 GROUP BY block_height ORDER BY block_height").fetchall()
    finally:
        conn.close()
    return {int(h): bh for (h, bh) in rows}


def _storage(port, addr):
    d = _rest(port, "vm/contract/%s" % addr)
    return {s["key"]: s["value"] for s in d.get("storage", [])}, d


def _probe_code():
    """RV32I probe: SSTORE(0, keccak256(payload)) ; SSTORE(1, ecrecover(payload, sig)). calldata = payload(32) | sig(65)."""
    from asmtools import Asm, A0, A3, S0, T0, T1, T3
    a = Asm()
    a.mv(S0, A0)                          # calldata ptr
    a.li(A3, 32); a.li(T1, 4096)
    a.keccak256(S0, A3, T1)               # [4096] = keccak256(payload)
    a.lw(T0, T1, 0); a.li(A3, 0); a.sstore(A3, T0)     # slot0 = first word of the keccak hash
    a.addi(T0, S0, 32); a.li(T3, 4128)
    a.ecrecover(T1, T0, T3)               # [4128] = recovered ETH address (hash from [4096])
    a.lw(T0, T3, 0); a.li(A3, 1); a.sstore(A3, T0)     # slot1 = first word of the recovered address
    a.halt()
    return a.assemble()


@pytest.mark.skipif(not os.environ.get("BISMUTH_RUN_MULTINODE"),
                    reason="heavy 3-node harness; run with BISMUTH_RUN_MULTINODE=1")
def test_bridge_artifacts_agree_across_nodes(tmp_path):
    if not os.path.exists(os.path.join(ROOT, "config_custom.txt")):
        shutil.copy(os.path.join(ROOT, "tests/config_custom.txt"), os.path.join(ROOT, "config_custom.txt"))
    import bridge_vault

    dir_a, dir_b, dir_c = str(tmp_path / "a"), str(tmp_path / "b"), str(tmp_path / "c")
    A_S, A_R, B_S, B_R, C_S, C_R = 4080, 4081, 4082, 4083, 4084, 4085
    procs = []
    try:
        pa, la = _start_node(dir_a, A_S, A_R, {})
        procs.append((pa, la))
        assert _await(lambda: _port_open(A_S), 90), "A socket\n" + _tail(os.path.join(dir_a, "node.log"))
        assert _await(lambda: _tip(A_R) >= 1, 90), "A REST\n" + _tail(os.path.join(dir_a, "node.log"))

        from _lite_client import LiteClient
        import vm_engine
        ca = LiteClient(os.path.join(ROOT, "wallet.der"), port=A_S)
        ca.mine(16)                                            # cross the regnet hf2 fork (window 5 / boundary 10)
        fork = _rest(A_R, "fork").get("fork_height")
        while fork is not None and _tip(A_R) <= int(fork) + 1:
            ca.mine(2)
        assert fork is not None and _tip(A_R) > int(fork), "fork not active"

        def _deploy(code_hex):
            before = set(_rest(A_R, "vm/contracts").get("contracts", []))
            ca.send(ca.address, 1, "vm:deploy", code_hex)
            for _ in range(8):
                ca.mine(2); time.sleep(0.2)
                new = set(_rest(A_R, "vm/contracts").get("contracts", [])) - before
                if new:
                    return new.pop()
            return None

        # --- peg-in vault: deploy + LOCK 5 BIS naming an Ethereum recipient ---
        vault = _deploy(bridge_vault.build().hex())
        assert vault, "vault did not deploy"
        eth_recipient = bytes.fromhex("f5cb350b40726b5bcf170d12e162b6193b291b41")    # live wBIS ERC-20 addr (20 bytes)
        ca.send(vm_engine.VM_SINK, 5.0, "vm:call", vault + ":" + eth_recipient.hex())  # value -> vault custody
        for _ in range(10):                                       # mine to include the lock tx, then poll
            ca.mine(2); time.sleep(0.2)
            if int(_rest(A_R, "vm/contract/%s" % vault).get("balance", 0)) == 500000000:
                break
        assert int(_rest(A_R, "vm/contract/%s" % vault)["balance"]) == 500000000, "lock not custodied on A"

        # --- crypto-syscall probe: deploy + call (stores keccak + ecrecover outputs) ---
        probe = _deploy(_probe_code().hex())
        assert probe, "probe did not deploy"
        pk = coincurve.PrivateKey(secret=b"\x55" * 32)
        payload = _kec(b"multinode-bridge-probe")
        sig = pk.sign_recoverable(_kec(payload), hasher=None)
        ca.send(ca.address, 0, "vm:call", probe + ":" + (payload + sig).hex())
        for _ in range(10):                                       # mine to include the probe call, then poll
            ca.mine(2); time.sleep(0.2)
            if len(_storage(A_R, probe)[0]) >= 2:
                break
        a_probe, _ = _storage(A_R, probe)
        assert len(a_probe) >= 2, "probe did not store both syscall outputs on A: %s" % a_probe

        a_tip = _tip(A_R)
        print("A tip=%d  vault=%s  probe=%s" % (a_tip, vault, probe))

        # --- B, C: empty ledgers, reconstruct A's chain over REST api_sync ---
        src = {"BISMUTH_API_SYNC": "1", "BISMUTH_API_SYNC_SOURCE": "127.0.0.1:%d" % A_R}
        pb, lb = _start_node(dir_b, B_S, B_R, src)
        pc, lc = _start_node(dir_c, C_S, C_R, src)
        procs += [(pb, lb), (pc, lc)]
        for nm, sp, rp, dd in (("B", B_S, B_R, dir_b), ("C", C_S, C_R, dir_c)):
            assert _await(lambda: _port_open(sp), 90), "%s socket\n%s" % (nm, _tail(os.path.join(dd, "node.log")))
            assert _await(lambda: _tip(rp) >= 1, 90), "%s REST\n%s" % (nm, _tail(os.path.join(dd, "node.log")))
        caught = _await(lambda: _tip(B_R) >= a_tip and _tip(C_R) >= a_tip, 220, poll=2.0)
        b_tip, c_tip = _tip(B_R), _tip(C_R)
        assert caught, "B/C did not catch up: A=%d B=%d C=%d\n--B--\n%s\n--C--\n%s" % (
            a_tip, b_tip, c_tip, _tail(os.path.join(dir_b, "node.log")), _tail(os.path.join(dir_c, "node.log")))
        tip = min(a_tip, b_tip, c_tip)
        ports = {"A": A_R, "B": B_R, "C": C_R}

        # 1) CHAIN PARITY
        ha = _block_hashes(os.path.join(dir_a, "regmode.db"))
        hb = _block_hashes(os.path.join(dir_b, "regmode.db"))
        hc = _block_hashes(os.path.join(dir_c, "regmode.db"))
        for h in range(1, tip + 1):
            assert ha.get(h) == hb.get(h) == hc.get(h), "block %d hash mismatch" % h
        print("CHAIN PARITY ok: blocks 1..%d byte-identical across A,B,C" % tip)

        # 2) COMMITTED MERKLE ROOT (Stage 2b)
        roots = {nm: _rest(p, "vm/contracts").get("state_root") for nm, p in ports.items()}
        assert roots["A"] == roots["B"] == roots["C"] and roots["A"] and len(roots["A"]) == 64, \
            "committed merkle root disagrees: %s" % roots
        print("COMMITTED ROOT ok: merkle state_root %s identical across A,B,C" % roots["A"])

        # 3) PEG-IN VAULT: custody + lock record identical across nodes
        vbal = {nm: int(_rest(p, "vm/contract/%s" % vault)["balance"]) for nm, p in ports.items()}
        assert vbal["A"] == vbal["B"] == vbal["C"] == 500000000, "vault custody disagrees: %s" % vbal
        vstore = {nm: _storage(p, vault)[0] for nm, p in ports.items()}
        assert vstore["A"] == vstore["B"] == vstore["C"], "vault lock record disagrees: %s" % vstore
        assert vstore["A"].get("16") == "500000000", "lock amount slot wrong/absent: %s" % vstore["A"]  # base=1*16
        print("VAULT ok: custody 5 BIS + lock record identical across A,B,C (slots=%s)" % sorted(vstore["A"]))

        # 4) CRYPTO SYSCALLS: probe storage (keccak + ecrecover outputs) identical across nodes
        pstore = {nm: _storage(p, probe)[0] for nm, p in ports.items()}
        assert pstore["A"] == pstore["B"] == pstore["C"], "syscall outputs diverged across nodes: %s" % pstore
        assert pstore["A"].get("0") and pstore["A"].get("1"), "keccak/ecrecover did not store: %s" % pstore["A"]
        print("CRYPTO SYSCALLS ok: keccak256+ecrecover outputs identical across A,B,C (slot0=%s slot1=%s)"
              % (pstore["A"].get("0"), pstore["A"].get("1")))

        print("\nALL BRIDGE MULTINODE CHECKS PASSED at tip %d: chain parity + committed merkle root + vault "
              "custody/lock + crypto-syscall determinism, across 3 independent ledgers." % tip)
    finally:
        for p, lg in procs:
            try:
                p.terminate(); p.wait(timeout=10)
            except Exception:
                try: p.kill()
                except Exception: pass
            try: lg.close()
            except Exception: pass
