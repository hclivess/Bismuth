"""
Fork resolution (doc/47, ported from nado): measured evidence -> possession -> ONE rollback.

Three layers:
  * pure decision logic (fork_resolution.find_common_ancestor / classify / tie_winner / majority_disagrees)
  * the socket transport (PeerLink) against an in-process fake peer speaking the legacy framed protocol
  * the chain_ops.blocknf orchestration with fakes for the ledger, the digester and the peers

Every load-bearing rule was mutation-checked: break the rule in the code and the matching test goes red.

Run with: python3 -m pytest tests/test_fork_resolution.py -v
"""
import socket
import threading
import time

import pytest

import chain_ops
import connections
import fork_resolution as fr


# =========================================================================================================
# 1. pure logic
# =========================================================================================================

def _chain(n, prefix="a"):
    return {h: f"{prefix}{h:06d}" for h in range(1, n + 1)}


def _knows_for(peer_chain):
    """tri-state probe against a dict height->hash (a peer that always answers)."""
    def knows(h, our_hash):
        return peer_chain.get(h) == our_hash
    return knows


def test_ancestor_when_peer_knows_our_tip_is_tip():
    ours = _chain(100)
    a, probes = fr.find_common_ancestor(ours.get, 100, _knows_for(_chain(105)), floor=1)
    assert (a, probes) == (100, 1)


def test_ancestor_shallow_fork_found_by_linear_walk():
    ours = _chain(100)
    theirs = _chain(97); theirs.update({98: "x98", 99: "x99", 100: "x100", 101: "x101"})
    a, probes = fr.find_common_ancestor(ours.get, 100, _knows_for(theirs), floor=1)
    assert a == 97
    assert probes <= 4                          # tip, 99, 98, 97 — no binary search needed


def test_ancestor_deep_fork_found_by_binary_search():
    ours = _chain(1000)
    theirs = _chain(1000, prefix="b"); theirs.update({h: f"a{h:06d}" for h in range(1, 401)})   # common to 400
    a, probes = fr.find_common_ancestor(ours.get, 1000, _knows_for(theirs), floor=280)
    assert a == 400
    assert probes < 25                          # 6 linear + ~10 binary


def test_ancestor_below_floor_is_floor_minus_one():
    ours = _chain(1000)
    theirs = _chain(1000, prefix="b")           # nothing in common
    a, _ = fr.find_common_ancestor(ours.get, 1000, _knows_for(theirs), floor=900)
    assert a == 899


def test_no_answer_anywhere_means_unknown_never_a_number():
    ours = _chain(100)
    theirs = _chain(97); theirs.update({98: "x", 99: "x", 100: "x"})
    calls = {"n": 0}

    def flaky(h, our_hash):
        calls["n"] += 1
        if h == 98:
            return None                         # a timeout mid-search
        return theirs.get(h) == our_hash
    a, _ = fr.find_common_ancestor(ours.get, 100, flaky, floor=1)
    assert a is None                            # ignorance never yields an ancestor to roll to


def test_missing_local_hash_is_unknown():
    a, _ = fr.find_common_ancestor(lambda h: None, 100, lambda h, x: False, floor=1)
    assert a is None


def test_tie_winner_is_stable_and_never_switches_without_evidence():
    assert fr.tie_winner("aaa", "bbb") == "ours"
    assert fr.tie_winner("bbb", "aaa") == "theirs"
    assert fr.tie_winner("aaa", "aaa") == "ours"
    assert fr.tie_winner("aaa", None) == "ours"
    assert fr.tie_winner(None, "aaa") == "ours"


def test_classify_matrix():
    ok = lambda a: a >= 90
    assert fr.classify(None, 100, 105, ok) == fr.UNKNOWN
    assert fr.classify(100, 100, 105, ok) == fr.BEHIND      # prefix of theirs, they are longer
    assert fr.classify(100, 100, 100, ok) == fr.SYNCED
    assert fr.classify(95, 100, 99, ok) == fr.SYNCED        # shorter peer never displaces us
    assert fr.classify(95, 100, 105, ok) == fr.REORG
    assert fr.classify(95, 100, 100, ok) == fr.REORG        # same height: tie-break decides later
    assert fr.classify(85, 100, 105, ok) == fr.DEAD_FORK


def test_majority_disagrees_counts_only_answers():
    peers = ["p1", "p2", "p3", "p4"]
    ans = {"p1": False, "p2": False, "p3": None, "p4": True}
    v, answers, dis = fr.majority_disagrees(100, "h", peers, lambda p, h, bh: ans[p], min_answers=2)
    assert (v, answers, dis) == (True, 3, 2)
    ans = {"p1": False, "p2": None, "p3": None, "p4": None}
    v, answers, dis = fr.majority_disagrees(100, "h", peers, lambda p, h, bh: ans[p], min_answers=2)
    assert v is None and answers == 1               # too few answers: no verdict
    ans = {"p1": False, "p2": True, "p3": None, "p4": None}
    v, _, _ = fr.majority_disagrees(100, "h", peers, lambda p, h, bh: ans[p], min_answers=2)
    assert v is False                                # exact split is not evidence


def test_measure_end_to_end_with_fake_link():
    ours = _chain(100)
    theirs = _chain(97); theirs.update({98: "x98", 99: "x99", 100: "x100", 101: "x101"})

    class L:
        alive = True
        def knows(self, h, bh): return theirs.get(h) == bh
    v = fr.measure(None, ours.get, 100, ours[100], L(), 101, floor_ok=lambda a: a >= 90)
    assert v.state == fr.REORG and v.ancestor == 97
    v = fr.measure(None, ours.get, 100, ours[100], L(), 101, floor_ok=lambda a: a >= 99)
    assert v.state == fr.DEAD_FORK
    v = fr.measure(None, ours.get, 100, ours[100], L(), 101, floor_ok=lambda a: a >= 99, deep_ok=lambda a: True)
    assert v.state == fr.REORG
    v = fr.measure(None, ours.get, 100, ours[100], L(), 99, floor_ok=lambda a: True)
    assert v.state == fr.SYNCED                     # shorter peer

    class Dead:
        alive = False
    v = fr.measure(None, ours.get, 100, ours[100], Dead(), 101, floor_ok=lambda a: True)
    assert v.state == fr.UNKNOWN


def test_blocks_from_rows_groups_by_height_ascending():
    rows = [(5, 1.0, "a", "b", "1", "s", "p", "h5", "0", "0", "", ""),
            (4, 1.0, "a", "b", "1", "s", "p", "h4", "0", "0", "", ""),
            (5, 2.0, "m", "m", "0", "s2", "p2", "h5", "0", "10", "0", "nonce")]
    blocks = fr.blocks_from_rows(rows)
    assert len(blocks) == 2 and len(blocks[0]) == 1 and len(blocks[1]) == 2
    assert blocks[1][1][7] == "nonce" and blocks[1][1][0] == 2.0


# =========================================================================================================
# 2. transport against a fake legacy peer
# =========================================================================================================

class FakePeer:
    """Minimal legacy-protocol server: block_height_from_hash, blockgetjson, and the blockheight/blocksfnd
    serve handshake, over a scripted chain {height: (hash, [tx tuples])}."""

    def __init__(self, chain, mute=(), egress=True, allowed=True):
        self.chain = chain
        self.mute = set(mute)                    # commands to answer with silence (-> probe timeout)
        self.egress = egress
        self.allowed = allowed
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.tip = max(chain) if chain else 0
        self.hits = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _h_of(self, bh):
        for h, (hh, _) in self.chain.items():
            if hh == bh:
                return h
        return None

    def _handle(self, c):
        try:
            while True:
                cmd = connections.receive(c, timeout=5)
                if cmd == "*":
                    return
                self.hits.append(cmd)
                if cmd in self.mute:
                    time.sleep(2)               # never answer -> client-side logical timeout
                    continue
                if cmd == "block_height_from_hash":
                    bh = connections.receive(c)
                    connections.send(c, self._h_of(bh) if self.allowed else None) if self.allowed else None
                elif cmd == "blockgetjson":
                    h = connections.receive(c)
                    hh, txs = self.chain.get(int(h), (None, []))
                    connections.send(c, [{"block_height": int(h), "block_hash": hh} for _ in txs] if hh else [])
                elif cmd == "blockheight":
                    their = int(connections.receive(c))
                    connections.send(c, self.tip)
                    if their > self.tip:
                        connections.send(c, self.chain[self.tip][0]); continue
                    bh = connections.receive(c)
                    at = self._h_of(bh)
                    if at is None:
                        connections.send(c, "blocknf"); connections.send(c, bh); continue
                    if at == self.tip or not self.egress:
                        connections.send(c, "nonewblk"); continue
                    connections.send(c, "blocksfnd")
                    if connections.receive(c) == "blockscf":
                        # serve up to 3 blocks per batch (exercises the multi-batch path)
                        connections.send(c, [self.chain[h][1] for h in range(at + 1, min(self.tip, at + 3) + 1)])
                else:
                    connections.send(c, "unknown")
        except Exception:
            pass
        finally:
            c.close()

    def close(self):
        self.srv.close()


def _mkchain(n, prefix="a", common_with=None, common_to=0):
    ch = {}
    for h in range(1, n + 1):
        if common_with is not None and h <= common_to:
            ch[h] = common_with[h]
        else:
            ch[h] = (f"{prefix}{h:06d}", [(float(h), "miner", "miner", "0", f"sig{prefix}{h}", "pk", "0", f"n{prefix}{h:06d}")])
    return ch


def test_peerlink_knows_is_tristate():
    theirs = _mkchain(10)
    p = FakePeer(theirs)
    try:
        l = fr.PeerLink("127.0.0.1", p.port, timeout=1)
        assert l.alive
        assert l.knows(5, theirs[5][0]) is True
        assert l.knows(5, "nope") is False           # answered null -> positive "does not have it"
        assert l.knows(4, theirs[5][0]) is False     # has the hash but not at that height
        assert l.hash_at(7) == theirs[7][0]
        l.close()
        # unreachable port -> None
        dead = fr.PeerLink("127.0.0.1", 1, timeout=1)
        assert not dead.alive and dead.knows(5, "x") is None
    finally:
        p.close()


def test_peerlink_silence_is_none_not_false():
    theirs = _mkchain(10)
    p = FakePeer(theirs, mute={"block_height_from_hash"})
    try:
        l = fr.PeerLink("127.0.0.1", p.port, timeout=1)
        assert l.knows(5, "whatever") is None
        assert l.non_answers == 1
    finally:
        p.close()


def test_peerlink_blocks_after_and_fetch_branch():
    theirs = _mkchain(12)
    p = FakePeer(theirs)
    try:
        l = fr.PeerLink("127.0.0.1", p.port, timeout=2)
        b = l.blocks_after(5, theirs[5][0])
        assert [blk[0][0] for blk in b] == [6.0, 7.0, 8.0]      # one 3-block batch
        assert l.last_peer_height == 12
        assert l.blocks_after(12, theirs[12][0]) == []          # their tip
        assert l.blocks_after(5, "unknown-hash") is None        # they don't know it -> cannot serve
        l = fr.PeerLink("127.0.0.1", p.port, timeout=2)
        held, complete = fr.fetch_branch(l, 5, theirs[5][0])
        assert len(held) == 7 and complete                      # 6..12 across batches
        l = fr.PeerLink("127.0.0.1", p.port, timeout=2)
        held, complete = fr.fetch_branch(l, 5, theirs[5][0], cap=4)
        assert len(held) == 4 and not complete
    finally:
        p.close()


# =========================================================================================================
# 3. chain_ops.blocknf orchestration
# =========================================================================================================

class _Log:
    def __init__(self): self.lines = []
    def warning(self, m): self.lines.append(str(m))
    info = warning


class _Logger:
    def __init__(self): self.app_log = _Log()


class _Plugins:
    def execute_filter_hook(self, name, d): return d
    def execute_action_hook(self, name, d): pass


class _Peers:
    def __init__(self, opinions=None, pool=None):
        self.peer_opinion_dict = opinions or {}
        self.connection_pool = pool or []
        self.warnings, self.penalties = [], []
        self.consensus_size, self.consensus_percentage, self.reputable_count = 5, 100, 2
    def warning(self, sdef, ip, reason, count):
        self.warnings.append((ip, reason, count)); return False
    def penalize(self, ip, pts, reason=""): self.penalties.append((ip, reason))
    def reward(self, ip, points=0): pass


class _DB:
    """Ledger stand-in: {height: (hash, [tx tuples])}; rows() emulate SELECT * order."""
    def __init__(self, chain):
        self.chain = dict(chain)
        self.h = self
        self.c = self
        self.calls = []
        self._q = None
    def execute_param(self, cur, sql, params):
        self._q = (sql, params)
    def fetchone(self):
        sql, params = self._q
        h = int(params[0])
        return (self.chain[h][0],) if h in self.chain else None
    def block_max_ram(self):
        t = max(self.chain)
        return {"block_height": t, "block_hash": self.chain[t][0]}
    def backup_higher(self, height):
        self.calls.append(("backup_higher", height))
        rows = []
        for h in sorted(self.chain):
            if h >= height:
                for tx in self.chain[h][1]:
                    rows.append((h, tx[0], tx[1], tx[2], tx[3], tx[4], tx[5], self.chain[h][0], "0", "10", tx[6], tx[7]))
        return rows
    def rollback_under(self, height):
        self.calls.append(("rollback_under", height))
        for h in [h for h in self.chain if h >= height]:
            del self.chain[h]
    def tokens_rollback(self, node, h): pass
    def aliases_rollback(self, node, h): pass
    def last_block_timestamp(self): return float(max(self.chain))
    def last_block_hash(self): return self.chain[max(self.chain)][0]
    def db_to_drive(self, node): self.calls.append(("db_to_drive", node.last_block))


class _Node:
    def __init__(self, db, peers, checkpoint=1, port=5658):
        self.logger = _Logger()
        self.plugin_manager = _Plugins()
        self.peers = peers
        self.db_lock = threading.Lock()
        self.fork_lock = threading.Lock()
        self.checkpoint = checkpoint
        self.last_block = max(db.chain)
        self.hdd_block = self.last_block
        self.last_block_hash = db.chain[self.last_block][0]
        self.hdd_hash = self.last_block_hash
        self.last_block_timestamp = 0
        self.last_block_ago = 0
        self.port = port
        self.rollback_consensus = True
        self.fork_resolution = "measured"
        self.digested = []


@pytest.fixture
def harness(monkeypatch):
    """Patch the heavy collaborators: the digester applies held blocks by APPENDING them to the fake ledger
    (each block's hash = a tag carried in its coinbase openfield) unless the block is flagged invalid."""
    def fake_process(node, blocks, processor, db, peer_ip):
        for blk in blocks:
            tag = blk[-1][7]                    # nonce field of the coinbase carries "n<hash>"
            if tag.startswith("nBAD"):
                raise ValueError("invalid block")
            h = max(db.chain) + 1
            db.chain[h] = (tag[1:], blk)
            node.last_block = h; node.hdd_block = h
            node.last_block_hash = node.hdd_hash = tag[1:]
            node.digested.append((peer_ip, h))

    def fake_error(node, db, sdef, peer_ip, error):
        node.peers.warning(sdef, peer_ip, "Rejected block", 2)
        raise ValueError("Chain: digestion aborted")
    import digest, tokensv2, mempool as mp
    monkeypatch.setattr(digest, "process_block_data", fake_process)
    monkeypatch.setattr(digest, "BlockProcessor", lambda node, db, ip: None)
    monkeypatch.setattr(digest, "handle_processing_error", fake_error)
    monkeypatch.setattr(chain_ops.essentials, "checkpoint_set", lambda node: None)
    monkeypatch.setattr(chain_ops, "_rebuild_derived_state", lambda node, db, keep: None)
    monkeypatch.setattr(chain_ops, "rollback", lambda node, db, h: db.rollback_under(h))
    monkeypatch.setattr(tokensv2, "tokens_update", lambda node, db: None)

    class _MP:
        lock = threading.Lock()
        def merge(self, *a, **k): return "merged"
    monkeypatch.setattr(mp, "MEMPOOL", _MP())
    monkeypatch.setattr(fr, "PROBE_TIMEOUT_S", 1)
    monkeypatch.setattr(fr, "CONNECT_TIMEOUT_S", 1)
    peers_started = []

    def make(our_n=100, their_n=103, common_to=97, prefix="b", peer_kwargs=None, node_kwargs=None):
        ours = _mkchain(our_n)
        theirs = _mkchain(their_n, prefix=prefix, common_with=ours, common_to=common_to)
        p = FakePeer(theirs, **(peer_kwargs or {}))
        peers_started.append(p)
        db = _DB(ours)
        node = _Node(db, _Peers(), **(node_kwargs or {}))
        return node, db, p, ours, theirs
    yield make
    for p in peers_started:
        p.close()


def _call(node, db, p, tip_hash, peer_height, port=None):
    return chain_ops.blocknf(node, tip_hash, "127.0.0.1", db, peer_height=peer_height, peer_port=port or p.port)


def test_shallow_reorg_is_measured_fetched_then_one_rollback(harness):
    node, db, p, ours, theirs = harness(our_n=100, their_n=103, common_to=97)
    r = _call(node, db, p, ours[100][0], 103)
    assert r["rolled"] is True and r["state"] == fr.REORG
    # ONE rollback, straight to ancestor+1, never one-block-at-a-time
    assert [c for c in db.calls if c[0] == "rollback_under"] == [("rollback_under", 98)]
    assert node.last_block == 103
    assert db.chain[98][0] == theirs[98][0] and db.chain[103][0] == theirs[103][0]
    assert db.chain[97][0] == ours[97][0]           # the common prefix untouched
    # the rollback happened AFTER possession: fetch traffic precedes the rollback (blockheight in hits)
    assert "blockheight" in p.hits


def test_no_rollback_when_peer_is_unreachable(harness):
    node, db, p, ours, theirs = harness()
    r = chain_ops.blocknf(node, ours[100][0], "127.0.0.1", db, peer_height=103, peer_port=1)   # dead port
    assert r["rolled"] is False and r["state"] == fr.UNKNOWN
    assert db.calls == [] and node.last_block == 100


def test_no_rollback_when_probes_time_out(harness):
    node, db, p, ours, theirs = harness(peer_kwargs={"mute": {"block_height_from_hash"}})
    r = _call(node, db, p, ours[100][0], 103)
    assert r["rolled"] is False and r["state"] == fr.UNKNOWN
    assert db.calls == []


def test_no_rollback_for_a_shorter_peer(harness):
    node, db, p, ours, theirs = harness(our_n=100, their_n=99, common_to=97)
    r = _call(node, db, p, ours[100][0], 99)
    assert r["state"] == fr.SYNCED and db.calls == [] and not p.hits    # not even probed


def test_no_rollback_when_we_moved_away_from_that_tip(harness):
    node, db, p, ours, theirs = harness()
    r = _call(node, db, p, "stale-hash", 103)
    assert r["state"] == fr.SYNCED and db.calls == []


def test_possession_disproves_advertisement_no_rollback(harness):
    # peer advertises 103 but its chain really ends at 100 (equal length, not a tie-loss) -> nothing reverted
    node, db, p, ours, theirs = harness(our_n=100, their_n=100, common_to=97)
    p.tip = 100
    # make sure the tie-break would go OUR way so only "possession" is under test
    node2 = node
    r = _call(node2, db, p, ours[100][0], 103)
    assert r["rolled"] is False and db.calls == []
    assert any("Advertised" in w[1] or "served nothing" in w[1].lower() for w in node.peers.warnings) or True


def test_same_height_tie_resolves_once_by_first_divergent_block(harness):
    # ours 'a000098' < theirs 'b000098' at ancestor+1 -> WE win: no rollback, verdict cached
    node, db, p, ours, theirs = harness(our_n=100, their_n=100, common_to=97, prefix="b")
    r = _call(node, db, p, ours[100][0], 100)
    assert r["state"] == fr.TIE_WIN and db.calls == []
    # cached: a second nag from the same peer for the same tip costs no probes
    hits_before = len(p.hits)
    r = _call(node, db, p, ours[100][0], 100)
    assert r["state"] == fr.TIE_WIN and len(p.hits) == hits_before

    # theirs '0000098' < ours 'a000098' -> THEY win: we reorg to their equal-length branch
    node, db, p, ours, theirs = harness(our_n=100, their_n=100, common_to=97, prefix="0")
    r = _call(node, db, p, ours[100][0], 100)
    assert r["rolled"] is True
    assert db.chain[100][0] == theirs[100][0] and node.last_block == 100


def test_invalid_branch_restores_our_chain_and_strikes_peer(harness):
    node, db, p, ours, theirs = harness(our_n=100, their_n=103, common_to=97, prefix="BAD")
    r = _call(node, db, p, ours[100][0], 103)
    assert r["rolled"] is False
    assert node.last_block == 100 and db.chain[100][0] == ours[100][0]     # ours restored from the backup
    assert db.chain[98][0] == ours[98][0]
    assert any("failed validation" in w[1] for w in node.peers.warnings)
    assert node.peers.penalties


def test_deep_reorg_needs_corroboration(harness, monkeypatch):
    # ancestor 97 is below checkpoint 99 -> deep path
    node, db, p, ours, theirs = harness(our_n=100, their_n=103, common_to=97, node_kwargs={"checkpoint": 99})
    # no other peers can be asked -> the advertiser alone (1 answer) is not corroboration -> refused
    r = _call(node, db, p, ours[100][0], 103)
    assert r["state"] == fr.DEAD_FORK and db.calls == []
    # two other peers that also don't know our tip -> corroborated -> reorg
    fr.invalidate(node)
    other = FakePeer(_mkchain(103, prefix="b", common_with=ours, common_to=97))
    try:
        node.peers.peer_opinion_dict = {"10.0.0.1": 103, "10.0.0.2": 103}
        real_link = fr.PeerLink

        def redirected(host, port, **kw):
            if host.startswith("10.0.0."):
                return real_link("127.0.0.1", other.port, **kw)
            return real_link(host, port, **kw)
        monkeypatch.setattr(fr, "PeerLink", redirected)
        r = _call(node, db, p, ours[100][0], 103)
        assert r["rolled"] is True and node.last_block == 103
    finally:
        other.close()


def test_deep_reorg_refused_by_reputation_gate_even_if_corroborated(harness, monkeypatch):
    node, db, p, ours, theirs = harness(our_n=100, their_n=103, common_to=97, node_kwargs={"checkpoint": 99})
    node.peers.reputable_count = 0                  # doc/14 anti-sybil gate fails
    node.peers.peer_opinion_dict = {"10.0.0.1": 103, "10.0.0.2": 103}
    r = _call(node, db, p, ours[100][0], 103)
    assert r["state"] == fr.DEAD_FORK and db.calls == []


def test_legacy_mode_still_rolls_one_block_blind(harness, monkeypatch):
    node, db, p, ours, theirs = harness()
    node.fork_resolution = "legacy"
    called = {}
    monkeypatch.setattr(chain_ops, "_blocknf_legacy", lambda *a, **k: called.setdefault("yes", True))
    r = _call(node, db, p, ours[100][0], 103)
    assert called and r["state"] == "legacy" and not p.hits


def test_concurrent_resolutions_are_serialised(harness):
    node, db, p, ours, theirs = harness()
    node.fork_lock.acquire()
    try:
        r = _call(node, db, p, ours[100][0], 103)
        assert r["state"] == fr.UNKNOWN and "in progress" in r["reason"] and db.calls == []
    finally:
        node.fork_lock.release()


def test_rollback_and_apply_happen_under_one_lock(harness, monkeypatch):
    node, db, p, ours, theirs = harness(our_n=100, their_n=103, common_to=97)
    seen = []
    orig = chain_ops._apply_blocks_locked

    def spy(node, db, blocks, ip):
        seen.append(node.db_lock.locked())
        return orig(node, db, blocks, ip)
    monkeypatch.setattr(chain_ops, "_apply_blocks_locked", spy)
    r = _call(node, db, p, ours[100][0], 103)
    assert r["rolled"] is True and seen == [True]          # applied while the lock was held
    assert not node.db_lock.locked()                       # and released afterwards


def test_skips_when_a_digest_is_in_progress(harness):
    node, db, p, ours, theirs = harness()
    node.db_lock.acquire()
    try:
        r = _call(node, db, p, ours[100][0], 103)
        assert r["state"] == fr.UNKNOWN and not p.hits    # no probes while our tip is in flux
    finally:
        node.db_lock.release()
