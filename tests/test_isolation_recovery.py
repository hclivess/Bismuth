"""
Fast-recovery-from-isolation tests for the outbound dial path (peershandler.client_loop +
PeersPoolMixin + the ConnectionManager cadence constant).

These lock in the fix for the live-mainnet stall where, after a restart or losing its peers, a node's
OUTBOUND connections collapsed to 0-1 and STAYED there for hours: the per-peer retry back-off
(add_try: 30s -> 1m -> 2m -> 5m) meant that once all peers were disconnected the node could not
re-dial them for minutes, and the connection manager only looped every 30s.

The fix: when outbound connections are at/below OUTBOUND_RECOVERY_FLOOR, client_loop bypasses the
back-off (reset_tried(aggressive=True)) and re-dials EVERY known peer in the same pass, recently-
responsive peers first, and the ConnectionManager shortens its sleep so this repeats within seconds.

Pure unit tests: a real Peers instance, but NO node, NO sockets, NO disk (the file-I/O maintenance
helpers are stubbed so only the dial decision is exercised). Consensus/sync code is untouched.

Run with: python3 -m pytest tests/test_isolation_recovery.py -v
"""
import threading
import types

import peershandler
from peershandler import Peers, OUTBOUND_RECOVERY_FLOOR


class _Log:
    def info(self, *a):
        pass

    def warning(self, *a):
        pass


class _TestPeers(Peers):
    # Peers declares __slots__, so instances are not monkeypatchable; a subclass WITHOUT __slots__ gets
    # a __dict__, letting us override the disk/network maintenance helpers so client_loop exercises only
    # the in-memory dial-decision logic. The dial path itself (the code under test) is fully inherited.
    def peers_get(self, *a, **k):
        return {}

    def peers_test(self, *a, **k):
        return None


def _make_peers(known_peers, ip_to_mainnet=None):
    """Build a real Peers (dial logic intact) with a minimal config and no disk/socket side effects."""
    config = types.SimpleNamespace(
        regnet=False, testnet=False, version="mainnet",
        port="5658", node_ip="203.0.113.7",
        banlist=[], whitelist=[], ban_threshold=10, accept_peers=True,
        thread_limit=64, nodes_ban_reset=5,
    )
    p = _TestPeers(_Log(), config=config, node=types.SimpleNamespace(IS_STOPPING=False))
    p.peer_dict = dict(known_peers)
    if ip_to_mainnet:
        p.ip_to_mainnet = dict(ip_to_mainnet)
    return p


def _record_dials(p):
    """Replace the worker target with a recorder and pin thread bookkeeping so the dial loop's
    `threading.active_count()` gate is always open. Returns the list that captures dialed (host, port)."""
    dialed = []

    def fake_worker(host, port, node):
        dialed.append((host, port))

    # client_loop spawns threads that call this_target; our fake target just records and returns.
    return dialed, fake_worker


def test_isolated_node_redials_all_known_peers_in_one_pass():
    peers = {f"10.0.0.{i}": 5658 for i in range(1, 6)}  # 5 known peers
    p = _make_peers(peers)
    # Simulate the stall: every known peer is deep in back-off (5 min out) AND zero outbound conns.
    from time import time as _t
    p.tried = {f"10.0.0.{i}:5658": (3, _t() + 300) for i in range(1, 6)}
    assert len(p._connection_pool_set) == 0  # isolated

    dialed, fake_worker = _record_dials(p)
    p.client_loop(p.node, this_target=fake_worker)
    # Threads are daemon; give them a moment to run the (trivial) recorder.
    for t in threading.enumerate():
        if t.name.startswith("out_"):
            t.join(timeout=2)

    # Every known peer must have been re-dialed THIS pass despite the 5-minute back-off.
    assert sorted(dialed) == sorted((ip, 5658) for ip in peers), dialed
    # And the back-off must have been wiped (then re-armed by add_try for the next pass).
    assert all(v[0] == 1 for v in p.tried.values()), p.tried  # all reset to first-try


def test_well_connected_node_keeps_backoff_and_does_not_bypass():
    peers = {f"10.0.0.{i}": 5658 for i in range(1, 6)}
    p = _make_peers(peers)
    # Above the floor: pretend we already hold plenty of outbound connections.
    for i in range(1, OUTBOUND_RECOVERY_FLOOR + 3):
        p._connection_pool_set.add(f"192.168.0.{i}:5658")
    assert len(p._connection_pool_set) > OUTBOUND_RECOVERY_FLOOR
    # All known peers are cooling down; the gentle steady state must RESPECT that (no aggressive bypass).
    from time import time as _t
    p.tried = {f"10.0.0.{i}:5658": (2, _t() + 300) for i in range(1, 6)}

    dialed, fake_worker = _record_dials(p)
    p.client_loop(p.node, this_target=fake_worker)
    for t in threading.enumerate():
        if t.name.startswith("out_"):
            t.join(timeout=2)

    # Not isolated -> back-off honored -> none of the cooling-down peers dialed.
    assert dialed == [], dialed
    # And their back-off timers are untouched (still tries==2, still in the future).
    assert all(v[0] == 2 for v in p.tried.values()), p.tried


def test_recovery_prioritizes_recently_responsive_peers():
    # ip_to_mainnet holds peers we have completed a handshake with => recently responsive.
    peers = {f"10.0.0.{i}": 5658 for i in range(1, 7)}
    proven_ips = {"10.0.0.2": "mainnet", "10.0.0.5": "mainnet"}
    p = _make_peers(peers, ip_to_mainnet=proven_ips)
    ordered = list(p._order_by_recency(peers).keys())
    # The two proven peers must come before any unproven one.
    assert set(ordered[:2]) == set(proven_ips), ordered
    assert set(ordered) == set(peers), "ordering must not drop or add peers"


def test_recovery_floor_and_cadence_constants_are_sane():
    # The recovery path must trigger only when genuinely low, and the recovery cadence must be much
    # tighter than the normal one (else "seconds not minutes" doesn't hold).
    import connectionmanager
    assert 1 <= OUTBOUND_RECOVERY_FLOOR <= 8
    assert connectionmanager.OUTBOUND_RECOVERY_FLOOR is OUTBOUND_RECOVERY_FLOOR
    assert connectionmanager.RECOVERY_LOOP_SECONDS < connectionmanager.NORMAL_LOOP_SECONDS
    assert connectionmanager.RECOVERY_LOOP_SECONDS >= 1  # still polite, not a busy-spin
