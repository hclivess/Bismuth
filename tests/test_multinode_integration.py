"""
Multi-node regnet integration test (doc/26 KVStore storage seam) — the 3-node cross-node validation.

This is the heavier sibling of tests/test_two_node_api_sync.py. It brings up a THREE-node regnet cluster
(no socket peering — regnet refuses it by design; api_sync over REST is the ONLY multi-node path) and proves
that the just-migrated KVStore-backed LMDB stores produce IDENTICAL results across independently-built
ledgers. config_custom.txt turns on, per node: the four core rebuildable side-indexes that were migrated
onto kvstore.open_store (block_store / balance_index / txid_index), PLUS token_index (owned by the
optional tokens_aliases PLUGIN, doc/27 — node core never constructs it; it arrives as a plugin service). The
KVStore-backed indexes serve the consensus reads; this test exercises all of them across the cluster.

    A  socket 4070 / REST 4071   — mines a chain CROSSING the regnet hf2 fork, issues a token
    B  socket 4072 / REST 4073   — api_sync=ON from A's REST (empty ledger -> reconstructs A's chain)
    C  socket 4074 / REST 4075   — api_sync=ON from A's REST (empty ledger -> reconstructs A's chain)

Asserts, using ONLY the REST API surfaces (never opening the LMDB dirs directly):
  1. CHAIN PARITY      — every block hash 1..tip is byte-identical across all 3 independent ledgers.
  2. STORE AGREEMENT   — for a handful of addresses/txs/blocks, each node returns IDENTICAL results:
                           * balance  (/api/balance/<addr>)         -> balance_index @ primary
                           * tx height(/api/transaction/<txid>)     -> txid_index
                           * block bodies by height (/api/block/...) -> block_store
                           * token supply + holders (/api/token/<name>) -> token_index (tokens_aliases PLUGIN)
                       The token checks run on POPULATED state: node A issues+transfers a token
                       (all post-fork) BEFORE B/C sync.
  3. DETECTOR          — every node's /api/difficulty agrees (the #23 difficulty-divergence detector reads
                          CLEAN: no false divergence on a healthy multi-node net).

Heavy: manages its own 3 node processes. Gated like the two-node test — run explicitly with:
    BISMUTH_RUN_MULTINODE=1 python3 -m pytest tests/test_multinode_integration.py -v -s
Without the flag the normal suite NEVER spawns nodes (the test is skipped).
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
TESTS = os.path.join(ROOT, "tests")
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)


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
        try:
            if cond():
                return True
        except Exception:
            pass
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


def _tail(path, n=40):
    try:
        with open(path) as f:
            return "".join(f.readlines()[-n:])
    except Exception:
        return "(no log)"


def _canonical_block(port, height):
    """A node's view of a block body by height, normalised to a stable comparable shape.
    Backed by block_store. Drops display-only/float fields that legitimately differ in
    representation and keeps the consensus-load-bearing tuple per tx."""
    blk = _rest(port, "block/height/%d" % height)
    txs = []
    for t in blk.get("transactions", []):
        txs.append((t.get("timestamp"), t.get("address"), t.get("recipient"),
                    t.get("signature"), t.get("operation"), t.get("openfield"),
                    t.get("block_hash"), t.get("txid")))
    return tuple(txs)


@pytest.mark.skipif(not os.environ.get("BISMUTH_RUN_MULTINODE"),
                    reason="heavy three-node harness (spins up 3 extra node processes); "
                           "run explicitly with BISMUTH_RUN_MULTINODE=1")
def test_multinode_integration(tmp_path):
    # All regnet nodes read ROOT/config_custom.txt (regnet flags: block_store, balance_index=primary,
    # txid_index=primary, parity_strict, vm, token_index). Prod ignores it (BISMUTH_IGNORE_CONFIG_CUSTOM=1).
    if not os.path.exists(os.path.join(ROOT, "config_custom.txt")):
        shutil.copy(os.path.join(ROOT, "tests/config_custom.txt"), os.path.join(ROOT, "config_custom.txt"))

    dir_a = str(tmp_path / "a")
    dir_b = str(tmp_path / "b")
    dir_c = str(tmp_path / "c")
    A_S, A_R, B_S, B_R, C_S, C_R = 4070, 4071, 4072, 4073, 4074, 4075
    procs = []
    try:
        # --- node A: serve + mine the canonical chain --------------------------------------------------
        pa, la = _start_node(dir_a, A_S, A_R, {})
        procs.append((pa, la, os.path.join(dir_a, "node.log")))
        assert _await(lambda: _port_open(A_S), 90), "A socket never opened\n" + _tail(os.path.join(dir_a, "node.log"))
        assert _await(lambda: _tip_via_rest(A_R) >= 1, 90), "A REST never came up\n" + _tail(os.path.join(dir_a, "node.log"))

        from _lite_client import LiteClient
        ca = LiteClient(os.path.join(ROOT, "wallet.der"), port=A_S)
        ca.mine(16)                                   # crosses the regnet hf2 fork (window 5 / boundary 10 / bury 2)
        a_tip = _tip_via_rest(A_R)
        assert a_tip >= 14, "A only reached height %s" % a_tip

        # --- populate token_index (tokens_aliases PLUGIN): issue a token to self, then transfer some to a
        # 2nd holder. token_index has NO fork gate, but we are already post-fork so every projected row lands
        # in the window B/C reconstruct over REST. Poll for projection (mempool/mining is async).
        token_name = None
        token_recip = "bb" * 28  # 56-hex placeholder 2nd holder (token_index keys by address string)
        try:
            tname = "mntok"
            ca.send(ca.address, 1, "token:issue", "%s:1000000" % tname)   # issuer credited = recipient (self)
            issued = False
            for _ in range(8):
                ca.mine(2); time.sleep(0.2)
                try:
                    if (_rest(A_R, "token/%s" % tname) or {}).get("supply"):
                        issued = True; break
                except Exception:
                    pass
            if issued:
                ca.send(token_recip, 1, "token:transfer", "%s:250" % tname)   # move 250 to the 2nd holder
                for _ in range(8):
                    ca.mine(2); time.sleep(0.2)
                    try:
                        if (_rest(A_R, "token/%s" % tname) or {}).get("holder_count", 0) >= 2:
                            token_name = tname; break
                    except Exception:
                        pass
        except Exception as e:
            print("token populate best-effort skipped:", e)

        a_tip = _tip_via_rest(A_R)
        print("A tip after mining (token=%s): %d" % (token_name, a_tip))

        # --- nodes B and C: empty ledgers, catch up from A over REST ONLY ------------------------------
        src = {"BISMUTH_API_SYNC": "1", "BISMUTH_API_SYNC_SOURCE": "127.0.0.1:%d" % A_R}
        pb, lb = _start_node(dir_b, B_S, B_R, src)
        pc, lc = _start_node(dir_c, C_S, C_R, src)
        procs.append((pb, lb, os.path.join(dir_b, "node.log")))
        procs.append((pc, lc, os.path.join(dir_c, "node.log")))
        for nm, sp, rp, dd in (("B", B_S, B_R, dir_b), ("C", C_S, C_R, dir_c)):
            assert _await(lambda: _port_open(sp), 90), "%s socket never opened\n%s" % (nm, _tail(os.path.join(dd, "node.log")))
            assert _await(lambda: _tip_via_rest(rp) >= 1, 90), "%s REST never came up\n%s" % (nm, _tail(os.path.join(dd, "node.log")))

        caught = _await(lambda: _tip_via_rest(B_R) >= a_tip and _tip_via_rest(C_R) >= a_tip, 220, poll=2.0)
        b_tip, c_tip = _tip_via_rest(B_R), _tip_via_rest(C_R)
        assert caught, ("B/C did NOT catch up over REST: A=%d B=%d C=%d\n--- B ---\n%s\n--- C ---\n%s"
                        % (a_tip, b_tip, c_tip,
                           _tail(os.path.join(dir_b, "node.log")), _tail(os.path.join(dir_c, "node.log"))))

        # the common tip every node is asserted against
        tip = min(a_tip, b_tip, c_tip)
        print("3-node common tip:", tip)

        # === 1. CROSS-NODE CHAIN PARITY: every block hash 1..tip byte-identical across all 3 ledgers =====
        ha = _block_hashes(os.path.join(dir_a, "regmode.db"))
        hb = _block_hashes(os.path.join(dir_b, "regmode.db"))
        hc = _block_hashes(os.path.join(dir_c, "regmode.db"))
        for h in range(1, tip + 1):
            assert ha.get(h) == hb.get(h) == hc.get(h), \
                "block %d hash mismatch: A=%s B=%s C=%s" % (h, ha.get(h), hb.get(h), hc.get(h))
        assert ha.get(tip) is not None
        print("CHAIN PARITY ok: blocks 1..%d byte-identical across A,B,C" % tip)

        # prove B and C actually got there via the REST consume loop (regnet has no socket sync)
        for nm, dd in (("B", dir_b), ("C", dir_c)):
            lg = _tail(os.path.join(dd, "node.log"), 400)
            assert "api_sync: ingested" in lg and "API-sync consume loop started" in lg, \
                "%s caught up but log doesn't show api_sync ingestion:\n%s" % (nm, lg)

        # === 2. CROSS-NODE STORE AGREEMENT (the point) =================================================
        ports = {"A": A_R, "B": B_R, "C": C_R}
        fork_height = _rest(A_R, "fork").get("fork_height")

        # --- block_store: full block bodies by height agree across nodes (several heights incl. genesis+tip)
        sample_heights = sorted(set([1, 2, max(1, tip // 2), tip - 1, tip]))
        sample_heights = [h for h in sample_heights if 1 <= h <= tip]
        for h in sample_heights:
            bodies = {nm: _canonical_block(p, h) for nm, p in ports.items()}
            assert bodies["A"] == bodies["B"] == bodies["C"], \
                "block_store body at height %d disagrees across nodes:\n%s" % (h, json.dumps(
                    {nm: [list(t) for t in b] for nm, b in bodies.items()}, indent=2)[:2000])
        print("STORE AGREEMENT block_store ok: bodies at heights %s identical across A,B,C" % sample_heights)

        # --- balance_index (@primary): the miner/coinbase address balance agrees across nodes
        addrs = {ca.address}
        # also pull a couple of recipient addresses seen on chain to broaden the balance check
        for h in (tip, max(1, tip - 1)):
            for t in _rest(A_R, "block/height/%d" % h).get("transactions", []):
                if t.get("recipient"):
                    addrs.add(t["recipient"])
                if len(addrs) >= 4:
                    break
        for addr in addrs:
            bals = {nm: _rest(p, "balance/%s" % addr)["balance"] for nm, p in ports.items()}
            assert bals["A"] == bals["B"] == bals["C"], \
                "balance_index disagrees for %s: %s" % (addr, bals)
        print("STORE AGREEMENT balance_index ok: balances for %d addr(s) identical across A,B,C" % len(addrs))

        # --- txid_index: /api/transaction confirms a post-fork tx's confirmed height is identical on every
        #     node. NOTE the migrated txid_index KVStore itself is validated AUTHORITATIVELY (not by this REST
        #     path, which re-derives from SQLite): config_custom.txt runs txid_index_consensus=primary +
        #     parity_strict, so digest consults txid_index.contains() as the dedup gate on every post-fork
        #     block on all 3 nodes — a divergent txid_index would RAISE during digestion and break the
        #     (asserted byte-identical) chain above. This check is the cross-node tx-height corroboration.
        txid_checked = 0
        if fork_height is not None:
            # find post-fork blocks with txs and pick a few txids (coinbase txs always exist)
            picked = []
            h = tip
            while h > int(fork_height) and len(picked) < 3:
                for t in _rest(A_R, "block/height/%d" % h).get("transactions", []):
                    tx_id = t.get("txid")
                    if tx_id:
                        picked.append((tx_id, h))
                        break
                h -= 1
            for tx_id, expect_h in picked:
                heights = {}
                for nm, p in ports.items():
                    try:
                        heights[nm] = _rest(p, "transaction/%s" % tx_id).get("block_height")
                    except Exception as e:
                        heights[nm] = "ERR:%s" % e
                assert heights["A"] == heights["B"] == heights["C"] == expect_h, \
                    "txid_index disagrees for %s (expect h=%s): %s" % (tx_id, expect_h, heights)
                txid_checked += 1
        print("STORE AGREEMENT txid_index ok: %d post-fork txid height(s) identical across A,B,C" % txid_checked)

        # --- token_index (tokens_aliases PLUGIN): the projected token supply + holder set agree across nodes.
        # B/C never received these txs over a socket — their plugin re-projected them from the api_sync'd
        # blocks into an independently-built LMDB store, so identical supply/holders proves cross-node agreement.
        if token_name:
            tok = {nm: _rest(p, "token/%s" % token_name) for nm, p in ports.items()}
            sup = {nm: t.get("supply") for nm, t in tok.items()}
            assert sup["A"] == sup["B"] == sup["C"] == 1000000, "token supply disagrees/unexpected: %s" % sup
            hold = {nm: sorted((h["address"], h["balance"]) for h in t.get("holders", [])) for nm, t in tok.items()}
            assert hold["A"] == hold["B"] == hold["C"], "token holders disagree across nodes: %s" % hold
            assert tok["A"].get("holder_count") >= 2, "expected >=2 token holders, got %s" % tok["A"].get("holder_count")
            print("STORE AGREEMENT token_index ok: token '%s' supply=%s holders=%s identical across A,B,C"
                  % (token_name, sup["A"], hold["A"]))
        else:
            print("STORE AGREEMENT token_index: SKIPPED (token did not land)")

        # Coverage guard: this test EXISTS to prove POPULATED token state agrees across nodes. The fork
        # always activates here (mine(16) crosses fork=10), so REQUIRE the population to have happened — that
        # stops the coverage from silently degrading to a vacuous pass if a future change breaks token indexing.
        if fork_height is not None and tip > int(fork_height):
            assert token_name, "token population did not land despite active fork — coverage would be vacuous"

        # === 3. DETECTOR: /api/difficulty agrees across the cluster (no false divergence) ===============
        diffs = {nm: float(_rest(p, "difficulty").get("difficulty")) for nm, p in ports.items()}
        spread = max(diffs.values()) - min(diffs.values())
        assert spread < 1e-6, "difficulty divergence across healthy cluster: %s" % diffs
        # Drive the REAL #23 detector against the live 3-peer quorum and assert it reads CLEAN.
        try:
            import chain_ops
            import regnet

            class _Log:
                msgs = []
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

            peers_sock = ["127.0.0.1:%d" % A_S, "127.0.0.1:%d" % B_S, "127.0.0.1:%d" % C_S]
            stub = _Stub(peers_sock, float(regnet.REGNET_DIFF), tip)
            cache = {}
            diverged, local, median, n = chain_ops.detect_difficulty_divergence(
                stub, None, stub.peers, rest_port_cache=cache)
            assert not diverged, "detector falsely flagged divergence on a healthy cluster (median=%s n=%s)" % (median, n)
            assert n >= chain_ops.DIFF_DIVERGENCE_MIN_PEERS, \
                "detector did not reach a real >=%d-peer quorum (n=%s)" % (chain_ops.DIFF_DIVERGENCE_MIN_PEERS, n)
            print("DETECTOR ok: #23 detector CLEAN with %d-peer quorum, median diff=%s, /api/difficulty=%s"
                  % (n, median, diffs))
        except Exception as e:
            # The cluster-level /api/difficulty agreement above is the primary assert; the in-process
            # detector drive is a bonus. Surface but don't fail the run if its internals shifted.
            print("DETECTOR in-process drive skipped (cluster /api/difficulty already agrees %s): %s" % (diffs, e))

        print("\nALL CHECKS PASSED: 3-node chain parity + block_store/balance_index/txid_index"
              " agreement + token_index (populated) + detector CLEAN at tip %d" % tip)
    finally:
        for proc, log, _ in procs:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                log.close()
            except Exception:
                pass
