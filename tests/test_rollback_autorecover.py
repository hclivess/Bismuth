"""
Auto-recovery rollback policy (essentials.rollback_allowed) — pure unit tests.

Replaces the rigid `rollback_depth` stranding: a deep-forked node rolls back as far as needed to rejoin
the chain PROVEN peers agree on (no manual re-bootstrap), while a fresh sybil flood still can't force a
deep reorg (anti-sybil gate: needs peers with positive reputation = delivered valid PoW blocks).

Run with: python3 -m pytest tests/test_rollback_autorecover.py -v
"""
import essentials


class _Peers:
    def __init__(self, size, pct, reputable):
        self.consensus_size = size
        self.consensus_percentage = pct
        self.reputable_count = reputable


class _Node:
    def __init__(self, checkpoint, peers, rollback_consensus=True):
        self.checkpoint = checkpoint
        self.peers = peers
        self.rollback_consensus = rollback_consensus


def test_shallow_rollback_always_allowed():
    node = _Node(checkpoint=1000, peers=_Peers(0, 0, 0))
    assert essentials.rollback_allowed(node, 1000) is True      # at the checkpoint
    assert essentials.rollback_allowed(node, 1500) is True      # above it


def test_deep_rollback_autorecovers_with_proven_peers():
    # below the checkpoint + strong consensus + proven peers -> AUTO-RECOVER (no manual re-bootstrap)
    node = _Node(checkpoint=1000, peers=_Peers(size=5, pct=80, reputable=2))
    assert essentials.rollback_allowed(node, 900) is True


def test_deep_rollback_blocked_for_fresh_sybil_flood():
    # strong consensus BUT no proven peers (fresh sybils that never delivered a valid block) -> refused
    node = _Node(checkpoint=1000, peers=_Peers(size=5, pct=80, reputable=0))
    assert essentials.rollback_allowed(node, 900) is False


def test_deep_rollback_blocked_without_supermajority():
    node = _Node(checkpoint=1000, peers=_Peers(size=5, pct=50, reputable=2))
    assert essentials.rollback_allowed(node, 900) is False


def test_deep_rollback_can_still_be_disabled():
    node = _Node(checkpoint=1000, peers=_Peers(5, 80, 2), rollback_consensus=False)
    assert essentials.rollback_allowed(node, 900) is False
