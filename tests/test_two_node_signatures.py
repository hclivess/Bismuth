"""
All signing schemes, end-to-end, on two nodes (doc/09 / doc/20 / doc/23).

For every on-chain signature scheme the node supports, create a REAL transaction on node A, mine it, and
confirm it paid its recipient (proves the scheme validated + was included). Then node B — a fresh node —
reconstructs A's whole chain over the REST API (api_sync) and is required to reach A's tip with EVERY
block hash identical, i.e. B independently re-validated every scheme's signature during digest.

Schemes covered (the ones wired into the node's address dispatch):
  RSA, ECDSA(secp256k1), ED25519, ML-DSA-44/65/87 (post-quantum), SECP256R1(P-256), native 2-of-3 multisig.

All scheme txs are POST-fork (A is mined past the hf2 activation first): ECDSA signs the content txid
(pubkey dropped), ED25519 signs the legacy buffer (pubkey dropped, recovered from address), and
RSA/ML-DSA/SECP256R1/multisig sign the legacy buffer with an explicit pubkey — exactly what
hd_wallet.sign_transaction / MultisigAccount build and what the digester verifies.

Heavy (spins up two extra node processes) — run explicitly:
    BISMUTH_RUN_TWONODE=1 python3 -m pytest tests/test_two_node_signatures.py -v -s
"""
import hashlib
import json
import os
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


def _tip(port):
    try:
        return int(_rest(port, "status").get("blocks", 0) or 0)
    except Exception:
        return -1


def _start_node(datadir, sock_port, rest_port, extra_env):
    os.makedirs(datadir, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "BISMUTH_REGNET_PORT": str(sock_port), "BISMUTH_REST_API_PORT": str(rest_port),
        "BISMUTH_REGNET_DB": os.path.join(datadir, "regmode.db"),
        "BISMUTH_REGNET_INDEX": os.path.join(datadir, "index_reg.db"),
        "BISMUTH_REGNET_PEERS": os.path.join(datadir, "peers_reg.txt"),
        "BISMUTH_MEMPOOL_PATH": os.path.join(datadir, "mempool.db"),
    })
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
    conn = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=20)
    conn.text_factory = str
    try:
        rows = conn.execute("SELECT block_height, block_hash FROM transactions "
                            "WHERE block_height > 0 GROUP BY block_height ORDER BY block_height").fetchall()
    finally:
        conn.close()
    return {int(h): bh for (h, bh) in rows}


def _seed(tag):
    return hashlib.sha256(tag.encode()).hexdigest()   # deterministic 64-hex seed


def _mine_until(ca, pred, rounds=40):
    for _ in range(rounds):
        if pred():
            return True
        ca.mine(2)
        time.sleep(0.2)
    return pred()


@pytest.mark.skipif(not os.environ.get("BISMUTH_RUN_TWONODE"),
                    reason="heavy two-node harness; run with BISMUTH_RUN_TWONODE=1")
def test_all_signature_schemes_two_node(tmp_path):
    if not os.path.exists(os.path.join(ROOT, "config_custom.txt")):
        import shutil
        shutil.copy(os.path.join(ROOT, "tests/config_custom.txt"), os.path.join(ROOT, "config_custom.txt"))

    from polysign.signer import SignerType
    from polysign.signerfactory import SignerFactory
    import hd_wallet
    from multisig_wallet import MultisigAccount

    dir_a, dir_b = str(tmp_path / "a"), str(tmp_path / "b")
    procs = []
    results = {}
    try:
        pa, la = _start_node(dir_a, 4040, 4041, {})
        procs.append((pa, la))
        assert _await(lambda: _tip(4041) >= 1, 90), "A never came up"

        from _lite_client import LiteClient
        ca = LiteClient(os.path.join(ROOT, "wallet.der"), port=4040)

        # mine past the hf2 fork so every scheme tx below is post-fork
        def _fork_active():
            try:
                d = _rest(4041, "fork")
                return d.get("fork_height") is not None and _tip(4041) > int(d["fork_height"])
            except Exception:
                return False
        assert _mine_until(ca, _fork_active, rounds=40), "A did not activate hf2"

        FRESH = lambda tag: SignerFactory.from_seed(_seed("dest-" + tag), signer_type=SignerType.ECDSA).address()

        # Spends are submitted over the REST write path (POST /api/transaction). The legacy socket mpinsert
        # frames fine for small txs but drops ("Socket EOF") on PQ-sized ML-DSA txs (~10 KB sig+pubkey); the
        # HTTP path carries any size and is the post-fork transport anyway. (A prior run confirmed the small
        # schemes also pass via the socket.)
        API = 4041

        # --- RSA: the wallet itself is RSA; a normal wallet send exercises it ---
        d = FRESH("rsa")
        ca.send_via_api(d, 1.0, api_port=API)
        results["RSA"] = _mine_until(ca, lambda: ca.balance(d) >= 1.0 - 1e-6)

        # --- single-sig schemes: fund a fresh address of that scheme, then spend FROM it ---
        SINGLE = [("ECDSA", SignerType.ECDSA), ("ED25519", SignerType.ED25519),
                  ("MLDSA44", SignerType.MLDSA44), ("MLDSA65", SignerType.MLDSA65),
                  ("MLDSA87", SignerType.MLDSA87), ("SECP256R1", SignerType.SECP256R1)]
        for label, st in SINGLE:
            try:
                s = SignerFactory.from_seed(_seed(label), signer_type=st)
                saddr = s.address()
                ca.send(saddr, 20.0)
                if not _mine_until(ca, lambda a=saddr: ca.balance(a) >= 20.0 - 1e-6):
                    results[label] = False
                    continue
                dest = FRESH(label)
                tx = hd_wallet.sign_transaction(s, "%.2f" % time.time(), saddr, dest, "1.0", post_fork=True)
                ca.api_submit(list(tx), api_port=API)
                results[label] = _mine_until(ca, lambda d=dest: ca.balance(d) >= 1.0 - 1e-6)
            except Exception as e:
                results[label] = "ERROR: %r" % e

        # --- native 2-of-3 multisig ---
        try:
            owners = [hd_wallet.HDWallet(bytes([0xD0 + i]) * 32).node_at(0) for i in range(3)]
            acct = MultisigAccount.from_owners(owners, 2)
            ca.send(acct.address, 20.0)
            if _mine_until(ca, lambda: ca.balance(acct.address) >= 20.0 - 1e-6):
                dest = FRESH("multisig")
                tx = acct.sign_transaction([owners[0], owners[1]], "%.2f" % time.time(), dest, 1.0, "", "")
                ca.api_submit(list(tx), api_port=API)
                results["MULTISIG"] = _mine_until(ca, lambda: ca.balance(dest) >= 1.0 - 1e-6)
            else:
                results["MULTISIG"] = False
        except Exception as e:
            results["MULTISIG"] = "ERROR: %r" % e

        a_tip = _tip(4041)

        # --- node B: reconstruct the whole chain (every scheme's tx) over REST, validate it matches ---
        pb, lb = _start_node(dir_b, 4042, 4043,
                             {"BISMUTH_API_SYNC": "1", "BISMUTH_API_SYNC_SOURCE": "127.0.0.1:4041"})
        procs.append((pb, lb))
        assert _await(lambda: _tip(4043) >= 1, 90), "B never came up"
        b_caught = _await(lambda: _tip(4043) >= a_tip, 180, poll=2.0)

        ha, hb = _block_hashes(os.path.join(dir_a, "regmode.db")), _block_hashes(os.path.join(dir_b, "regmode.db"))
        mismatch = [h for h in range(1, a_tip + 1) if ha.get(h) != hb.get(h)]

        print("\n=== signing schemes (node A inclusion) ===")
        for k, v in results.items():
            print("  %-10s %s" % (k, "PASS" if v is True else ("FAIL" if v is False else v)))
        print("=== node B re-validated all over REST: caught=%s tip A=%s B=%s mismatches=%d ==="
              % (b_caught, a_tip, _tip(4043), len(mismatch)))

        failed = [k for k, v in results.items() if v is not True]
        assert not failed, "schemes that did not pass on node A: %s" % failed
        assert b_caught and not mismatch, "node B did not faithfully re-validate (mismatch heights: %s)" % mismatch[:10]
    finally:
        for proc, log in procs:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            log.close()
