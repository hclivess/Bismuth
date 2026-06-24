"""
Unit tests for the peer-difficulty divergence detector + guarded self-heal (doc/35, #23).

These are PURE unit tests — no node process, no ledger, no network. We feed a fake ``node`` + fake
``peers`` and monkeypatch ``rest_client.get_capabilities`` / ``get_difficulty`` so the detector's HTTP
poll is deterministic. Covers: median/quorum/threshold, thin-data ABSTAIN, the effective threshold,
height-matching, and the heal guards (cooldown / max-heals / bounded-depth / once-per-boot) blocking a
2nd heal, plus the advisory-vs-heal target computation.

    python3 -m pytest tests/test_difficulty_divergence.py -v
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import chain_ops
import rest_client


class _Log:
    def __init__(self):
        self.msgs = []

    def warning(self, m):
        self.msgs.append(m)

    info = warning
    debug = warning


class _Logger:
    def __init__(self):
        self.app_log = _Log()


class _Peers:
    def __init__(self, pool, consensus=None, consensus_size=5, consensus_percentage=100, reputable_count=2):
        self.connection_pool = list(pool)
        self.consensus = consensus
        self.consensus_size = consensus_size
        self.consensus_percentage = consensus_percentage
        self.reputable_count = reputable_count


class _Node:
    """Minimal node stand-in: only the attributes the detector/heal touch."""
    def __init__(self, difficulty=None, last_block=1000, peers=None, ledger_path=None,
                 checkpoint=900, rollback_depth=30):
        self.difficulty = difficulty
        self.last_block = last_block
        self.peers = peers if peers is not None else _Peers([])
        self.ledger_path = ledger_path
        self.checkpoint = checkpoint
        self.rollback_depth = rollback_depth
        self.rollback_consensus = True
        self.logger = _Logger()
        self._diffheal_armed = False
        self.fork_height = None


def _diff_tuple(d, height):
    # mirrors difficulty()'s 8-tuple shape: difficulty first, block_height last
    return (d, d - 8, 60.0, d, 60.0, 0.0, 0.0, height)


def _patch_peers(monkeypatch, by_ip):
    """by_ip: {ip: (rest_diff, rest_height) or None}. Every ip reports rest_port=9999 capable.
    A value of None => the peer is not REST-capable (capabilities returns None)."""
    def fake_caps(host, port, timeout=rest_client.DEFAULT_TIMEOUT):
        if by_ip.get(host) is None:
            return None
        return {"rest_api": True, "rest_port": 9999}

    def fake_diff(host, port, timeout=rest_client.DEFAULT_TIMEOUT):
        v = by_ip.get(host)
        if v is None:
            return None
        d, h = v
        return {"difficulty": d, "diff_block_previous": d, "block_height": h}

    monkeypatch.setattr(rest_client, "get_capabilities", fake_caps)
    monkeypatch.setattr(rest_client, "get_difficulty", fake_diff)


# --------------------------------------------------------------------------- detector


def test_clean_when_local_matches_peer_median(monkeypatch):
    by_ip = {"1.1.1.1": (89.8, 1000), "2.2.2.2": (89.8, 1000), "3.3.3.3": (89.9, 1000)}
    _patch_peers(monkeypatch, by_ip)
    node = _Node(difficulty=_diff_tuple(89.8, 1000), last_block=1000,
                 peers=_Peers([f"{ip}:5658" for ip in by_ip]))
    diverged, local, median, n = chain_ops.detect_difficulty_divergence(node, None, node.peers)
    assert n == 3
    assert not diverged
    assert local == 89.8
    assert abs(median - 89.8) < 0.2


def test_diverged_when_local_below_peer_median(monkeypatch):
    # the incident: local stuck at 84.48 while the network agrees ~89.8
    by_ip = {"1.1.1.1": (89.8, 1000), "2.2.2.2": (89.8, 1000), "3.3.3.3": (89.9, 1000), "4.4.4.4": (89.7, 1000)}
    _patch_peers(monkeypatch, by_ip)
    node = _Node(difficulty=_diff_tuple(84.48, 1000), last_block=1000,
                 peers=_Peers([f"{ip}:5658" for ip in by_ip]))
    diverged, local, median, n = chain_ops.detect_difficulty_divergence(node, None, node.peers)
    assert diverged is True
    assert local == 84.48
    assert n == 4


def test_abstain_on_thin_data(monkeypatch):
    # only 2 height-matched peers < min_peers (3) -> ABSTAIN even though they disagree wildly with us
    by_ip = {"1.1.1.1": (89.8, 1000), "2.2.2.2": (89.8, 1000)}
    _patch_peers(monkeypatch, by_ip)
    node = _Node(difficulty=_diff_tuple(70.0, 1000), last_block=1000,
                 peers=_Peers([f"{ip}:5658" for ip in by_ip]))
    diverged, local, median, n = chain_ops.detect_difficulty_divergence(node, None, node.peers)
    assert n == 2
    assert diverged is False


def test_abstain_when_local_unknown(monkeypatch):
    by_ip = {"1.1.1.1": (89.8, 1000), "2.2.2.2": (89.8, 1000), "3.3.3.3": (89.9, 1000)}
    _patch_peers(monkeypatch, by_ip)
    node = _Node(difficulty=None, last_block=1000, peers=_Peers([f"{ip}:5658" for ip in by_ip]))
    diverged, local, median, n = chain_ops.detect_difficulty_divergence(node, None, node.peers)
    assert diverged is False
    assert local is None


def test_height_mismatched_peers_excluded(monkeypatch):
    # two peers are on a far-away tip (height 1500) -> excluded; remaining 1 < min_peers -> ABSTAIN
    by_ip = {"1.1.1.1": (89.8, 1000), "2.2.2.2": (89.8, 1500), "3.3.3.3": (89.9, 1500)}
    _patch_peers(monkeypatch, by_ip)
    node = _Node(difficulty=_diff_tuple(70.0, 1000), last_block=1000,
                 peers=_Peers([f"{ip}:5658" for ip in by_ip]))
    diverged, local, median, n = chain_ops.detect_difficulty_divergence(node, None, node.peers)
    assert n == 1
    assert diverged is False


def test_non_rest_peers_skipped(monkeypatch):
    # 2 peers have no REST API (caps None) -> not counted; remaining 3 form the quorum
    by_ip = {"1.1.1.1": (89.8, 1000), "2.2.2.2": (89.8, 1000), "3.3.3.3": (89.9, 1000),
             "4.4.4.4": None, "5.5.5.5": None}
    _patch_peers(monkeypatch, by_ip)
    node = _Node(difficulty=_diff_tuple(84.0, 1000), last_block=1000,
                 peers=_Peers([f"{ip}:5658" for ip in by_ip]))
    diverged, local, median, n = chain_ops.detect_difficulty_divergence(node, None, node.peers)
    assert n == 3
    assert diverged is True


def test_no_heal_when_peers_disagree_among_themselves(monkeypatch):
    # peers are split (no >=75% internal agreement) -> NOT diverged even though local differs from median
    by_ip = {"1.1.1.1": (80.0, 1000), "2.2.2.2": (85.0, 1000), "3.3.3.3": (90.0, 1000), "4.4.4.4": (95.0, 1000)}
    _patch_peers(monkeypatch, by_ip)
    node = _Node(difficulty=_diff_tuple(70.0, 1000), last_block=1000,
                 peers=_Peers([f"{ip}:5658" for ip in by_ip]))
    diverged, local, median, n = chain_ops.detect_difficulty_divergence(node, None, node.peers)
    assert n == 4
    assert diverged is False    # peers don't agree among themselves -> abstain on action


def test_threshold_is_max_of_abs_and_relative():
    # at high difficulty the 2% relative band dominates the 0.5 absolute floor
    assert chain_ops._diff_effective_threshold(100.0, 0.5) == pytest.approx(2.0)
    # at low difficulty the absolute floor dominates
    assert chain_ops._diff_effective_threshold(10.0, 0.5) == pytest.approx(0.5)


def test_poll_capped_at_max(monkeypatch):
    # 20 peers all clean+matched, but only DIFF_DIVERGENCE_MAX_POLL are polled
    by_ip = {f"10.0.0.{i}": (89.8, 1000) for i in range(20)}
    _patch_peers(monkeypatch, by_ip)
    node = _Node(difficulty=_diff_tuple(89.8, 1000), last_block=1000,
                 peers=_Peers([f"{ip}:5658" for ip in by_ip]))
    diverged, local, median, n = chain_ops.detect_difficulty_divergence(node, None, node.peers)
    assert n == chain_ops.DIFF_DIVERGENCE_MAX_POLL


# --------------------------------------------------------------------------- heal guards


def test_guards_cooldown_blocks_second_heal(tmp_path):
    import time as _t
    node = _Node(ledger_path=str(tmp_path / "regmode.db"))
    state = chain_ops.diffheal_state_read(node)
    state["last_heal_ts"] = _t.time() - 60     # 60s ago, < 1h cooldown
    ok, reason = chain_ops.diffheal_guards_ok(node, state)
    assert ok is False
    assert "cooldown" in reason


def test_guards_max_heals_blocks(tmp_path):
    import time as _t
    node = _Node(ledger_path=str(tmp_path / "regmode.db"))
    now = _t.time()
    state = chain_ops.diffheal_state_read(node)
    state["last_heal_ts"] = now - chain_ops.DIFF_HEAL_COOLDOWN_S - 1    # cooldown satisfied
    state["heals_24h"] = [now - 7200, now - 3600]                       # 2 in the last 24h == max
    ok, reason = chain_ops.diffheal_guards_ok(node, state)
    assert ok is False
    assert "max heals" in reason


def test_guards_once_per_boot_blocks(tmp_path):
    node = _Node(ledger_path=str(tmp_path / "regmode.db"))
    node._diffheal_armed = True
    state = chain_ops.diffheal_state_read(node)
    ok, reason = chain_ops.diffheal_guards_ok(node, state)
    assert ok is False
    assert "already armed" in reason


def test_guards_allow_when_clean(tmp_path):
    node = _Node(ledger_path=str(tmp_path / "regmode.db"))
    state = chain_ops.diffheal_state_read(node)
    ok, reason = chain_ops.diffheal_guards_ok(node, state)
    assert ok is True


def test_target_bounded_by_checkpoint(tmp_path):
    # tip 1000, depth 30 -> 970, above checkpoint 900 -> target 970
    node = _Node(ledger_path=str(tmp_path / "regmode.db"), last_block=1000, checkpoint=900, rollback_depth=30)
    state = chain_ops.diffheal_state_read(node)
    target = chain_ops.diffheal_target(node, state)
    assert target == 970


def test_lifetime_cap_blocks_forever_even_after_24h_window_reopens(tmp_path):
    # [review #1 CRITICAL] The no-restart-loop guarantee: a corruption that SURVIVES every resync must STOP
    # healing forever, not 2x/day. With heal_count at the lifetime cap, guards must block even with the rolling
    # 24h window fully re-opened, no cooldown, and `now` arbitrarily far in the future.
    node = _Node(ledger_path=str(tmp_path / "regmode.db"))
    state = chain_ops.diffheal_state_read(node)
    state["heal_count"] = chain_ops.DIFF_HEAL_LIFETIME_MAX     # lifetime budget exhausted
    state["last_heal_ts"] = 0                                  # cooldown satisfied
    state["heals_24h"] = []                                    # rolling window fully re-opened
    ok, reason = chain_ops.diffheal_guards_ok(node, state, now=10 ** 12)   # far future
    assert ok is False
    assert "lifetime" in reason


def test_confirmed_clean_resets_heal_budget(tmp_path):
    # [review #1/#3] Only a confirmed-CLEAN reading restores the budget: a FIXED corruption can heal again for
    # a future incident; a SURVIVING one never reads clean, never resets, and stays capped (advisory-only).
    node = _Node(ledger_path=str(tmp_path / "regmode.db"))
    state = chain_ops.diffheal_state_read(node)
    state["heal_count"] = chain_ops.DIFF_HEAL_LIFETIME_MAX
    state["heals_24h"] = [1.0, 2.0]
    chain_ops.diffheal_state_write(node, state)
    ok, _ = chain_ops.diffheal_guards_ok(node, chain_ops.diffheal_state_read(node), now=10 ** 12)
    assert ok is False                                        # capped before the clean reading
    chain_ops.diffheal_note_clean(node)                       # a confirmed-clean reading restores the budget
    s2 = chain_ops.diffheal_state_read(node)
    assert s2["heal_count"] == 0 and s2["heals_24h"] == []
    ok2, _ = chain_ops.diffheal_guards_ok(node, s2)
    assert ok2 is True


def test_rest_port_resolution_failure_is_not_cached(monkeypatch):
    # [review #2] A transient capabilities failure must be a cache MISS that retries, never a permanent
    # non-REST verdict that erodes the sample set toward perpetual abstain.
    import rest_client
    calls = {"n": 0}
    monkeypatch.setattr(rest_client, "get_capabilities", lambda h, p: (calls.__setitem__("n", calls["n"] + 1), None)[1])
    node = _Node()
    cache = {}
    assert chain_ops._resolve_rest_port(node, "1.2.3.4", 5555, cache) is None
    assert chain_ops._resolve_rest_port(node, "1.2.3.4", 5555, cache) is None
    assert "1.2.3.4" not in cache          # failure NOT memoized
    assert calls["n"] == 2                  # retried instead of returning a stale cached None


def test_target_clamped_to_checkpoint_floor(tmp_path):
    # checkpoint 995 -> max(1000-30, 995) == 995 (never below checkpoint)
    node = _Node(ledger_path=str(tmp_path / "regmode.db"), last_block=1000, checkpoint=995, rollback_depth=30)
    state = chain_ops.diffheal_state_read(node)
    target = chain_ops.diffheal_target(node, state)
    assert target == 995


def test_target_deepens_with_heal_count_capped(tmp_path):
    node = _Node(ledger_path=str(tmp_path / "regmode.db"), last_block=10000, checkpoint=0, rollback_depth=30)
    state = chain_ops.diffheal_state_read(node)
    state["heal_count"] = 0
    assert chain_ops.diffheal_target(node, state) == 10000 - 30
    state["heal_count"] = 1
    assert chain_ops.diffheal_target(node, state) == 10000 - 60
    state["heal_count"] = 2
    assert chain_ops.diffheal_target(node, state) == 10000 - 90
    # capped at 3 * rollback_depth even at higher heal_count
    state["heal_count"] = 9
    assert chain_ops.diffheal_target(node, state) == 10000 - 90


def test_target_none_when_rollback_not_allowed(tmp_path):
    # below checkpoint + anti-sybil gate fails (few peers / low consensus) -> None (advisory-only)
    peers = _Peers([], consensus_size=1, consensus_percentage=10, reputable_count=0)
    node = _Node(ledger_path=str(tmp_path / "regmode.db"), last_block=1000, checkpoint=2000,
                 rollback_depth=30, peers=peers)
    node.rollback_consensus = True
    state = chain_ops.diffheal_state_read(node)
    target = chain_ops.diffheal_target(node, state)
    assert target is None


# --------------------------------------------------------------------------- sidecar + arm


def test_sidecar_is_per_ledger(tmp_path):
    node_a = _Node(ledger_path=str(tmp_path / "regmode.db"))
    node_b = _Node(ledger_path=str(tmp_path / "ledger.db"))
    assert chain_ops._diffheal_sidecar_path(node_a) != chain_ops._diffheal_sidecar_path(node_b)
    assert chain_ops._diffheal_sidecar_path(node_a).endswith("regmode.db.diffheal.json")


class _DbLock:
    def __init__(self):
        self.held = False

    def acquire(self, blocking=True, timeout=-1):
        self.held = True
        return True

    def release(self):
        self.held = False


def test_arm_writes_rollback_file_and_records_heal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)            # rollback_to is written in cwd (consumed at startup)
    node = _Node(ledger_path=str(tmp_path / "regmode.db"), last_block=1000, checkpoint=900)
    node.db_lock = _DbLock()
    state = chain_ops.diffheal_state_read(node)
    target = chain_ops.diffheal_target(node, state)
    assert chain_ops.diffheal_arm(node, target) is True
    # rollback_to file written with the clamped target
    with open(os.path.join(str(tmp_path), "rollback_to")) as fh:
        assert int(fh.read().strip()) == target
    assert node._diffheal_armed is True
    # sidecar records the heal; a second arm is now blocked once-per-boot
    state2 = chain_ops.diffheal_state_read(node)
    assert state2["heal_count"] == 1
    assert state2["last_target"] == target
    assert len(state2["heals_24h"]) == 1
    ok, reason = chain_ops.diffheal_guards_ok(node, state2)
    assert ok is False and "already armed" in reason
