"""
Peer reputation / penalization (mixin) — hardens consensus ESTABLISHMENT (doc/14).

The legacy consensus is a flat 1-peer-1-vote `most_common` height with ad-hoc warnings. This adds a
reputation score per peer:
  * a peer that **lies about its height** or **feeds an invalid block** LOSES reputation (→ ban);
  * a peer that **delivers a valid block** GAINS it;
  * the agreed tip can be a reputation-**WEIGHTED** vote, so a flaky or adversarial peer counts for less
    than a proven one.

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
    for ip, height in opinions.items():
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
        score = clamp_score(self.reputation(ip), -abs(points))
        self._reputation[ip] = score
        self.app_log.warning(f"Reputation: {ip} -{abs(points)} ({reason}) -> {score}")
        if score <= REP_BAN_BELOW and ip not in self.banlist:
            self.banlist.append(ip)
            self.app_log.warning(f"Reputation: {ip} banned (score {score} <= {REP_BAN_BELOW})")
            return True
        return False

    def reward(self, ip, points=REWARD_VALID_BLOCK):
        """Raise a peer's reputation for good behaviour (bounded, whitelist-immune)."""
        if self.is_whitelisted(ip):
            return
        self._reputation[ip] = clamp_score(self.reputation(ip), abs(points))

    def reputation_weight(self, ip):
        """Positive consensus-vote weight from the score: a proven peer counts more, a suspect one less,
        but EVERY peer keeps a minimum voice (≥1) so no single peer can dictate the tip."""
        return max(1, self.reputation(ip) - REP_MIN + 1)

    @property
    def consensus_reputation_weighted(self):
        """The agreed tip weighted by reputation — resists a flood of low-rep peers lying about it."""
        return weighted_tip(self.peer_opinion_dict, self.reputation_weight)
