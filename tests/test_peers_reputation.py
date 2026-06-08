"""
Peer reputation / penalization (peers_reputation) — pure unit tests.

Hardens consensus establishment: lying about height / feeding invalid blocks costs reputation (→ ban),
valid blocks earn it, and the tip vote weights by reputation. These lock the attack-vector safety:
bounded scores, whitelist immunity, and a per-peer minimum vote — so penalization can never isolate the
node and no single peer can dictate the tip.

Run with: python3 -m pytest tests/test_peers_reputation.py -v
"""
import logging

import peers_reputation as R
from peers_reputation import PeersReputationMixin, clamp_score, weighted_tip


def test_clamp_score_is_bounded():
    assert clamp_score(0, 10) == 10
    assert clamp_score(0, -10) == -10
    assert clamp_score(R.REP_MAX, 50) == R.REP_MAX           # can't run away up
    assert clamp_score(R.REP_MIN, -50) == R.REP_MIN          # can't run away down


def test_weighted_tip_resists_lowrep_flood():
    opinions = {"a": 999, "b": 999, "c": 999, "trusted": 100}
    high = {"a": 1, "b": 1, "c": 1, "trusted": 50}
    assert weighted_tip(opinions, lambda ip: high[ip]) == 100   # proven peer outweighs the lying flood
    assert weighted_tip(opinions, lambda ip: 1) == 999          # equal weight -> plain majority
    assert weighted_tip({}, lambda ip: 1) == 0


class _Harness(PeersReputationMixin):
    """A minimal Peers-like object exercising the mixin without the full manager."""
    __slots__ = ('_reputation', 'banlist', 'whitelist', 'peer_opinion_dict', 'app_log')

    def __init__(self):
        self._reputation = {}
        self.banlist = []
        self.whitelist = ["127.0.0.1"]
        self.peer_opinion_dict = {}
        self.app_log = logging.getLogger("test")

    def is_whitelisted(self, ip, command=''):
        return ip in self.whitelist


def test_penalize_bans_but_whitelist_is_immune():
    h = _Harness()
    h.penalize("127.0.0.1", 1000, "x")                       # whitelisted -> immune
    assert "127.0.0.1" not in h.banlist and h.reputation("127.0.0.1") == 0

    banned = False
    for _ in range(10):
        banned = h.penalize("1.2.3.4", R.PENALTY_HEIGHT_LIE, "lie") or banned
    assert banned and "1.2.3.4" in h.banlist
    assert h.reputation("1.2.3.4") == R.REP_MIN              # bounded at the floor


def test_weight_keeps_a_minimum_voice_for_everyone():
    h = _Harness()
    assert h.reputation_weight("unseen") >= 1                # an unseen peer still votes
    h.reward("good", R.REWARD_VALID_BLOCK)
    h.penalize("bad", R.PENALTY_INVALID_BLOCK, "invalid block")
    assert h.reputation_weight("good") > h.reputation_weight("bad")   # proven > suspect
    assert h.reputation_weight("bad") >= 1                   # but a suspect peer is never fully silenced
