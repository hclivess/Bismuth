"""
Peer reputation / penalization (mixin) — hardens consensus ESTABLISHMENT (doc/14).

The legacy consensus is a flat 1-peer-1-vote `most_common` height with ad-hoc warnings. This adds a
reputation score per peer:
  * a peer that **lies about its height** or **feeds an invalid block** LOSES reputation (→ ban);
  * a peer that **delivers a valid block** GAINS it;
  * the agreed sync tip IS a reputation-**WEIGHTED** vote (wired into the "most common block" rule in
    node.py / worker.py via `consensus_reputation_weighted`), so a flaky or adversarial peer counts for
    less than a proven one. It reduces to the flat plurality when reputations are uniform, and validation
    still backstops whatever the node syncs to.

ATTACK-VECTOR SAFETY (standing rule):
  * scores are the node's OWN observations — a peer cannot inflate its own;
  * bounded to [REP_MIN, REP_MAX] — no runaway up or down;
  * **whitelist-immune** and a **per-peer minimum vote weight (≥1)** — so penalization can never isolate
    the node, and no single high-rep peer can unilaterally dictate the chain tip.
"""

REP_MIN = -100
REP_MAX = 100
REP_BAN_BELOW = -50          # score at/under which a peer is banned

# penalty / reward weights for specific behaviours
PENALTY_INVALID_BLOCK = 40   # served a block that failed validation — serious
PENALTY_HEIGHT_LIE = 20      # claimed a tip far above consensus it cannot back
PENALTY_TIMEOUT = 5          # dropped / timed out mid-exchange
REWARD_VALID_BLOCK = 5       # delivered a valid block


def clamp_score(current, delta):
    """Apply a bounded reputation delta."""
    return max(REP_MIN, min(REP_MAX, current + delta))


def weighted_tip(opinions, weight_of):
    """Reputation-weighted agreed height. ``opinions``: {ip: height}; ``weight_of(ip)`` -> positive
    weight. Returns the height carrying the most total weight (0 if none). A flood of low-weight peers
    lying about the tip is outvoted by fewer high-weight (proven) peers."""
    tally = {}
    # Snapshot first: peer_opinion_dict is mutated (add/pop) by other peer threads with no lock,
    # and iterating the live dict raises "dictionary changed size during iteration".
    for ip, height in list(opinions.items()):
        tally[height] = tally.get(height, 0) + weight_of(ip)
    return max(tally, key=tally.get) if tally else 0


class PeersReputationMixin:
    __slots__ = ()

    def reputation(self, ip):
        return self._reputation.get(ip, 0)

    def penalize(self, ip, points, reason=""):
        """Lower a peer's reputation (bounded, whitelist-immune). Bans it if the score reaches
        REP_BAN_BELOW. Returns True iff the peer was banned by this call."""
        if self.is_whitelisted(ip):
            return False
        # read-modify-write of _reputation (and the banlist append) under one lock: two peer threads
        # penalizing the same ip concurrently must not lose an update or double-append to the banlist.
        with self.peers_lock:
            score = clamp_score(self.reputation(ip), -abs(points))
            self._reputation[ip] = score
            banned = score <= REP_BAN_BELOW and ip not in self.banlist
            if banned:
                self.banlist.append(ip)
        self.app_log.warning(f"Reputation: {ip} -{abs(points)} ({reason}) -> {score}")
        if banned:
            self.app_log.warning(f"Reputation: {ip} banned (score {score} <= {REP_BAN_BELOW})")
        return banned

    def reward(self, ip, points=REWARD_VALID_BLOCK):
        """Raise a peer's reputation for good behaviour (bounded, whitelist-immune)."""
        if self.is_whitelisted(ip):
            return
        with self.peers_lock:
            self._reputation[ip] = clamp_score(self.reputation(ip), abs(points))

    def reputation_weight(self, ip):
        """Positive consensus-vote weight from the score: a proven peer counts more, a suspect one less,
        but EVERY peer keeps a minimum voice (≥1) so no single peer can dictate the tip."""
        return max(1, self.reputation(ip) - REP_MIN + 1)

    @property
    def consensus_reputation_weighted(self):
        """The agreed tip weighted by reputation — resists a flood of low-rep peers lying about it."""
        # Under the lock the whole tally (which iterates peer_opinion_dict and reads _reputation via
        # reputation_weight) sees a stable snapshot — no "changed size during iteration".
        with self.peers_lock:
            return weighted_tip(self.peer_opinion_dict, self.reputation_weight)

    @property
    def reputable_count(self):
        """How many peers have a PROVEN (positive) reputation — peers that have actually delivered
        valid PoW blocks. Gates deep auto-recovery rollbacks: a fresh sybil flood (no proven blocks)
        cannot reach the bar, so it cannot force a deep reorg."""
        with self.peers_lock:
            return sum(1 for s in self._reputation.values() if s > 0)
