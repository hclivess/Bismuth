"""
FORK RESOLUTION — measured evidence, then possession, then (and only then) a rollback.

Ported from nado's consensus consolidation (nado `ops/fork_resolution.py` + the emergency-sync gating,
`doc/finality.md` §3, 2026-08-17/18) and adapted to Bismuth's PoW longest-chain rule.

The legacy Bismuth reorg was a single peer's word: a peer whose advertised height equalled the pool
maximum said ``blocknf`` ("I don't have your tip hash") and the node rolled back ONE block, blind, then
asked again — one round trip and one destroyed block per step, before ever holding a single block of the
competing chain. Anyone able to advertise a height could make a node shed real blocks for free, a
same-height fork made BOTH sides roll back (each is the other's "not found"), and a fork deeper than a
handful of blocks was a rollback storm. That is exactly the disruption-for-free / rollback-storm class
nado measured live (2,609 rollbacks in a day on a healthy chain) and closed. The rules that transfer:

1. **Absence of information is never evidence of divergence.** Every probe is TRI-STATE: ``True`` (the
   peer serves our hash at that height), ``False`` (it ANSWERED and does not have it — positive evidence),
   ``None`` (unreachable / timeout / refused / malformed — evidence of nothing). ``None`` never rolls back.
2. **The rollback is bounded by a measured ancestor.** Before touching the ledger the node finds the
   highest height at which the peer still knows OUR hash (linear walk for the common shallow case, then a
   binary search — O(log depth) probes, all tri-state). Rolling past a proven ancestor is pure loss.
3. **Possession before rollback.** The competing branch is FETCHED FIRST (from the measured ancestor
   forward, through the ordinary ``blockheight``/``blocksfnd`` serve path), checked to be strictly longer
   than ours (Bismuth's chain rule) — and only then does the node roll back to the ancestor in ONE
   operation and apply the held branch through the one canonical apply path (``digest_block``: PoW,
   signatures, balances, hash linkage). A branch that fails full validation costs the advertiser a strike
   and us seconds: our own rows were backed up and are re-applied. An attacker must present a held,
   longer branch that survives full validation to cause ANY revert.
4. **Ties resolve once, at the first divergent block.** A same-height fork is decided by comparing the two
   branches' blocks at ancestor+1 — a value that never changes as the branches grow — so both sides compute
   the same permanent winner and exactly one side reorgs (the legacy code made both roll back and re-mine).
5. **Deep rollbacks need corroboration.** Below the node's rollback checkpoint the existing reputation +
   height-consensus gate (``essentials.rollback_allowed``, doc/14) is joined by a HASH-LEVEL majority
   check: a strict majority of the answering peers must also not know our tip. Ignorance (too few
   answers) refuses the deep reorg; it never permits it.

The pure decision functions here take probe callables and are fully unit-testable without a network;
``PeerLink`` is the small socket transport (existing, whitelisted-by-default legacy commands only — no
protocol change), and ``measure`` ties them together. The chain mutation itself stays in
``chain_ops.blocknf``.
"""
import time
from collections import namedtuple

import socks

import connections

# ---- verdict states ------------------------------------------------------------------------------------
BEHIND, SYNCED, REORG, DEAD_FORK, UNKNOWN, TIE_WIN = "behind", "synced", "reorg", "dead_fork", "unknown", "tie_win"

# ---- tunables --------------------------------------------------------------------------------------------
PROBE_TIMEOUT_S = 5            # one round trip; a peer that cannot answer in 5 s is "no answer", never "no"
CONNECT_TIMEOUT_S = 5
LINEAR_PROBES = 6              # walk back this many heights one-by-one before binary-searching (shallow reorgs)
MAX_PROBE_DEPTH = 720          # never search for an ancestor deeper than this below our tip (matches the
                               # consensus pool's "too old" horizon); deeper = refuse (dead fork), no local remedy
FETCH_CAP_BLOCKS = 1000        # possession cap: hold at most this many blocks of the competing branch before
                               # rolling; a majority branch longer than that is a long absence — roll to the
                               # measured ancestor and let ordinary forward sync finish (nado 39794b5a)
FETCH_BUDGET_S = 90            # wall-clock cap for the whole fetch
VERDICT_TTL_S = 60             # a verdict for (our tip hash, peer) is reused this long
MIN_ANSWERS_DEEP = 2           # corroborating answers required for a below-checkpoint (deep) reorg
MAX_PROBE_PEERS = 8            # peers polled for the deep-reorg corroboration
NON_ANSWER_STRIKES = 3         # consecutive non-answers from a peer before its advertisement is dropped

Verdict = namedtuple("Verdict", "state ancestor tip tip_hash peer_height probes reason")
_FAIL = object()               # transport failure sentinel — distinct from a peer legitimately answering null


# =========================================================================================================
# Pure decision logic
# =========================================================================================================

def tie_winner(ours_first_divergent, theirs_first_divergent):
    """STABLE same-height fork choice: which branch is canonical when two branches are exactly as long.

    Compares the FIRST DIVERGENT block (ancestor+1), a value that never changes as the branches grow, so
    both sides compute one permanent winner and exactly one side reorgs — once. (Comparing TIP hashes
    re-rolls every block; nado watched that see-saw for hours.) Returns "ours" / "theirs". Missing or
    equal inputs return "ours": no evidence never switches a node off its own chain."""
    if (not ours_first_divergent or not theirs_first_divergent
            or ours_first_divergent == theirs_first_divergent):
        return "ours"
    return "ours" if str(ours_first_divergent) < str(theirs_first_divergent) else "theirs"


def find_common_ancestor(our_hash_at, tip, knows, floor, linear=LINEAR_PROBES):
    """Highest height in [floor, tip] at which ``knows(height, our_hash)`` is True, using TRI-STATE probes.

    ``knows(h, our_hash)`` -> True (peer has our block at h) / False (answered: does not) / None (no
    answer). Returns ``(ancestor, probes)``:
      * ``ancestor == tip``  : the peer knows our tip — we are a prefix of its chain (BEHIND/SYNCED)
      * ``floor <= a < tip``  : proven divergence above ``a``
      * ``ancestor == floor-1``: we disagree even at the floor — the divergence is below everything we may
        search (dead fork / too deep)
      * ``ancestor is None`` : a probe returned no answer — UNKNOWN. Never a rollback.
    Walks back one height at a time for ``linear`` steps (the common shallow reorg costs 1-3 probes), then
    binary-searches the remainder (O(log depth))."""
    probes = 0

    def agrees(h):
        nonlocal probes
        probes += 1
        oh = our_hash_at(h)
        if not oh:
            return None
        return knows(h, oh)

    top = agrees(tip)
    if top is None:
        return None, probes
    if top:
        return tip, probes
    hi = tip                                     # invariant: disagree at hi
    # linear walk for the shallow case
    for _ in range(max(0, int(linear))):
        h = hi - 1
        if h < floor:
            return floor - 1, probes
        a = agrees(h)
        if a is None:
            return None, probes
        if a:
            return h, probes
        hi = h
    if hi <= floor:
        return floor - 1, probes
    bottom = agrees(floor)
    if bottom is None:
        return None, probes
    if not bottom:
        return floor - 1, probes
    lo = floor                                   # invariant: agree at lo, disagree at hi
    while hi - lo > 1:
        mid = (lo + hi) // 2
        a = agrees(mid)
        if a is None:
            return None, probes
        if a:
            lo = mid
        else:
            hi = mid
    return lo, probes


def majority_disagrees(tip, tip_hash, peers, knows, min_answers=MIN_ANSWERS_DEEP):
    """Deep-reorg corroboration: do a STRICT MAJORITY of the ANSWERING peers not know our tip?

    ``knows(peer, tip, tip_hash)`` is tri-state; non-answers are not evidence either way. Returns
    ``(verdict, answers, disagree)`` where verdict is True (majority disagrees), False (majority agrees or
    exact split — no evidence to revert), or None (fewer than ``min_answers`` answered)."""
    answers = disagree = 0
    for p in peers:
        r = knows(p, tip, tip_hash)
        if r is None:
            continue
        answers += 1
        if r is False:
            disagree += 1
    if answers < int(min_answers):
        return None, answers, disagree
    return (disagree * 2 > answers), answers, disagree


def classify(ancestor, tip, peer_height, floor_ok):
    """Ancestor -> the single action to take (pure arithmetic).

    ``floor_ok(ancestor)`` says whether a rollback to ``ancestor`` is permitted by the node's rollback
    policy (checkpoint / reputation-gated deep recovery)."""
    if ancestor is None:
        return UNKNOWN
    if ancestor >= tip:
        # our whole chain is a prefix of theirs — forward sync, NEVER a rollback (they either just have more
        # blocks, or answered inconsistently with their own blocknf)
        return BEHIND if peer_height > tip else SYNCED
    if peer_height < tip:
        return SYNCED                            # a shorter chain never displaces ours, whatever it knows
    if not floor_ok(ancestor):
        return DEAD_FORK
    return REORG


# =========================================================================================================
# Transport: one socket per peer, legacy commands only
# =========================================================================================================

class PeerLink:
    """A short-lived connection to a peer's LISTENING port, speaking the legacy framed protocol.

    Only pre-existing commands are used (``block_height_from_hash``, ``blockgetjson``, and the standard
    ``blockheight``/``blocksfnd`` serve handshake), so every deployed node — REST-capable or not — can be
    measured and fetched from without a protocol change. Every method is fail-soft and returns ``None`` on
    any transport problem: the caller reads that as "no answer", never as "no"."""

    def __init__(self, host, port, proxy=None, timeout=PROBE_TIMEOUT_S, connect_timeout=CONNECT_TIMEOUT_S):
        self.host, self.port = host, int(port)
        self.timeout = timeout
        self.sock = None
        self.non_answers = 0
        try:
            s = socks.socksocket()
            if proxy:
                s.setproxy(socks.PROXY_TYPE_SOCKS5, proxy[0], proxy[1])
            s.settimeout(connect_timeout)
            s.connect((self.host, self.port))
            s.settimeout(None)
            self.sock = s
        except Exception:
            self.sock = None

    @property
    def alive(self):
        return self.sock is not None

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _rpc(self, *frames):
        """Send frames, receive one reply. ``_FAIL`` on any transport failure (and the link is dropped) —
        a peer's own JSON ``null`` answer comes back as None and is a REAL answer."""
        if self.sock is None:
            return _FAIL
        try:
            for f in frames:
                connections.send(self.sock, f)
            r = connections.receive(self.sock, timeout=self.timeout)
            if r == "*":                         # logical timeout — the peer said nothing
                self.non_answers += 1
                self.close()
                return _FAIL
            self.non_answers = 0
            return r
        except Exception:
            self.non_answers += 1
            self.close()
            return _FAIL

    def knows(self, height, block_hash):
        """TRI-STATE: does the peer hold OUR block ``block_hash`` at ``height``?
        True = it reports that hash at that height; False = it ANSWERED and does not have it; None = no
        answer (link down, timeout, command refused, malformed)."""
        r = self._rpc("block_height_from_hash", block_hash)
        if r is _FAIL:
            return None
        if r is None or r in ("", "None"):
            return False                         # the peer ANSWERED: it has no such block hash
        try:
            return int(r) == int(height)
        except (TypeError, ValueError):
            return None

    def hash_at(self, height):
        """The peer's block hash at ``height`` (via ``blockgetjson``), or None."""
        r = self._rpc("blockgetjson", int(height))
        if r is _FAIL or not isinstance(r, list) or not r:
            return None
        try:
            return str(r[0]["block_hash"])
        except Exception:
            return None

    def blocks_after(self, height, block_hash):
        """Fetch the peer's blocks AFTER (height, block_hash) through the ordinary serve handshake:
        ``blockheight`` -> our height -> theirs -> our hash -> ``blocksfnd``/``blocknf``/``nonewblk``.
        Returns a list of digester-ready blocks (may be empty when the peer's tip IS that block), or
        None when the peer could not or would not serve (it does not know the hash, egress off, timeout).
        The peer's height is exposed on ``self.last_peer_height``."""
        self.last_peer_height = None
        if self.sock is None:
            return None
        try:
            connections.send(self.sock, "blockheight")
            connections.send(self.sock, int(height))
            their_h = connections.receive(self.sock, timeout=self.timeout)
            if their_h == "*":
                self.close(); return None
            self.last_peer_height = int(their_h)
            if self.last_peer_height < int(height):
                # they are BELOW the height we asked from: their handler now expects nothing more from us
                # (it sent its hash); we can't fetch from a shorter peer anyway
                self.close(); return None
            connections.send(self.sock, block_hash)
            r = connections.receive(self.sock, timeout=self.timeout)
            if r == "blocksfnd":
                connections.send(self.sock, "blockscf")
                blocks = connections.receive(self.sock, timeout=max(self.timeout, 30))
                if blocks == "*" or not isinstance(blocks, list):
                    self.close(); return None
                return blocks
            if r == "nonewblk":
                return []                        # they know it and it's their tip
            # "blocknf"/"blocknfhb" (+ hash) or anything else: cannot serve from there
            self.close()
            return None
        except Exception:
            self.close()
            return None


def _proxy_of(node):
    tm = getattr(node, "tor_manager", None)
    try:
        return tm.get_proxy() if tm is not None else None
    except Exception:
        return None


def candidate_peers(node, first_ip=None, first_port=None, min_height=0, limit=MAX_PROBE_PEERS):
    """``[(ip, port), …]`` to probe / fetch from: the triggering peer first, then outbound peers (whose
    listening port we know) whose advertised height is >= ``min_height``, then inbound-only peers on the
    default port. Bounded, de-duplicated."""
    out, seen = [], set()

    def add(ip, port):
        if ip and ip not in seen and len(out) < limit:
            seen.add(ip)
            out.append((ip, int(port)))

    default_port = int(getattr(node, "port", 5658) or 5658)
    peers = getattr(node, "peers", None)
    opinions = dict(getattr(peers, "peer_opinion_dict", {}) or {})
    pool = []
    for entry in list(getattr(peers, "connection_pool", []) or []):
        try:
            ip, port = str(entry).rsplit(":", 1)
            pool.append((ip, int(port)))
        except ValueError:
            continue
    if first_ip:
        # an inbound advertiser's listening port is unknown: use the outbound pool's port for that ip if we
        # also dial it, else the network default
        add(first_ip, first_port or next((pt for ip, pt in pool if ip == first_ip), default_port))
    for ip, port in pool:
        if opinions.get(ip, 0) >= min_height or ip == first_ip:
            add(ip, port)
    for ip, h in opinions.items():
        if h >= min_height:
            add(ip, default_port)
    return out


# =========================================================================================================
# Measurement against one advertising peer (+ optional deep corroboration)
# =========================================================================================================

def _cache_get(node, key):
    cache = getattr(node, "_fork_verdicts", None)
    if not cache:
        return None
    ent = cache.get(key)
    if ent and ent[1] > time.time():
        return ent[0]
    return None


def _cache_put(node, key, verdict, ttl=VERDICT_TTL_S):
    if not hasattr(node, "_fork_verdicts"):
        node._fork_verdicts = {}
    cache = node._fork_verdicts
    now = time.time()
    for k in [k for k, v in cache.items() if v[1] <= now]:
        cache.pop(k, None)
    cache[key] = (verdict, now + ttl)


def invalidate(node):
    """Drop every cached verdict — called whenever OUR tip changes (a verdict describes a tip that no
    longer exists; a stale REORG must never roll back the chain we just adopted)."""
    node._fork_verdicts = {}


def measure(node, our_hash_at, tip, tip_hash, link, peer_height, floor_ok, deep_ok=None):
    """One measurement of OUR chain against the peer behind ``link`` (which advertised ``peer_height`` and
    said it does not know our tip). Pure w.r.t. the chain: only reads ``our_hash_at``.

    ``floor_ok(ancestor)`` -> is a rollback to ``ancestor`` allowed by the shallow policy (checkpoint)?
    ``deep_ok(ancestor)``  -> for an ancestor the shallow policy refuses: may the deep-recovery policy
    (reputation gate + hash-majority corroboration) allow it? Optional; None = never.
    Returns a Verdict."""
    floor = max(1, int(tip) - MAX_PROBE_DEPTH)
    if peer_height is None or int(peer_height) < int(tip):
        return Verdict(SYNCED, None, tip, tip_hash, peer_height, 0, "peer is shorter than us")
    if not link.alive:
        return Verdict(UNKNOWN, None, tip, tip_hash, peer_height, 0, "peer unreachable — no evidence")
    ancestor, probes = find_common_ancestor(our_hash_at, tip, link.knows, floor)
    if ancestor is None:
        return Verdict(UNKNOWN, None, tip, tip_hash, peer_height, probes, "peer gave no usable answer")
    if ancestor < floor:
        return Verdict(DEAD_FORK, ancestor, tip, tip_hash, peer_height, probes,
                       f"divergence deeper than {MAX_PROBE_DEPTH} blocks — refusing")

    def _ok(a):
        if floor_ok(a):
            return True
        return bool(deep_ok(a)) if deep_ok is not None else False

    state = classify(ancestor, tip, int(peer_height), _ok)
    reason = {BEHIND: "we are a prefix of the peer's chain (peer answered inconsistently) — forward sync only",
              SYNCED: "peer knows our tip / is not longer",
              REORG: f"proven divergence above {ancestor}",
              DEAD_FORK: f"ancestor {ancestor} below the rollback floor and no corroborated deep recovery"}[state]
    return Verdict(state, ancestor, tip, tip_hash, int(peer_height), probes, reason)


def fetch_branch(link, ancestor, ancestor_hash, cap=FETCH_CAP_BLOCKS, budget_s=FETCH_BUDGET_S,
                 stop_height=None):
    """POSSESSION: pull the peer's branch from ``ancestor`` forward, batch by batch, until the peer has no
    more, ``cap`` blocks are held, or the budget expires. Returns ``(blocks, complete)`` — the digester-ready
    blocks (each a list of tx tuples) and whether the peer's whole branch was fetched — or ``(None, False)``
    when the peer could not serve from the ancestor at all.

    Linkage: each batch is requested by the LAST HASH WE HOLD, so a peer cannot splice unrelated blocks in
    without the digester's hash-chain check catching it; but the batches themselves carry no hashes (the
    serve path sends tx tuples), so between batches we chain on the peer's OWN advertised hash only via the
    height it reached — the digester re-derives and enforces every block hash on apply."""
    t0 = time.time()
    held = []
    cur_h, cur_hash = int(ancestor), ancestor_hash
    first = True
    while len(held) < cap and (time.time() - t0) < budget_s:
        batch = link.blocks_after(cur_h, cur_hash)
        if batch is None:
            return (None, False) if first else (held, False)
        first = False
        if not batch:
            return held, True                    # their tip reached
        for blk in batch:
            if not isinstance(blk, list) or not blk:
                return held, False
            held.append(blk)
            cur_h += 1
            if len(held) >= cap:
                return held, False               # possession cap: what we hold is a valid prefix to apply
        if stop_height is not None and cur_h >= stop_height:
            return held, True
        # to continue we need the hash of the last held block — recompute it the legacy way is not
        # possible without state (block hash covers the previous hash + converted tuples), so ask the peer
        # for it (blockgetjson); if refused, stop with what we hold (still a valid prefix to apply)
        nh = link.hash_at(cur_h)
        if not nh:
            return held, False
        cur_hash = nh
    return held, False


def blocks_from_rows(rows):
    """Group backed-up ledger rows (``SELECT *`` order: block_height, timestamp, address, recipient, amount,
    signature, public_key, block_hash, fee, reward, operation, openfield) into digester-ready blocks
    (ascending height, each a list of 8-field tx tuples in stored order)."""
    import amounts
    by_h = {}
    for r in rows:
        r = amounts.display_row(r)
        try:
            h = int(r[0])
        except (TypeError, ValueError, IndexError):
            continue
        if h <= 0:
            continue
        by_h.setdefault(h, []).append((r[1], r[2], r[3], r[4], r[5], r[6], r[10], r[11]))
    return [by_h[h] for h in sorted(by_h)]
