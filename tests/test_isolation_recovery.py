"""
Fast-recovery-from-isolation tests (peershandler.client_loop + PeersPoolMixin.reset_tried + the
ConnectionManager loop cadence).

These lock in the fix for the live-mainnet stall where, after a restart or losing its peers, a node's
OUTBOUND connections collapsed to 0-1 and STAYED there for a very long time: the per-peer retry back-off
(add_try: 30s -> 1m -> 2m -> 5m) meant that once all peers were disconnected the node could not re-dial
them for minutes, and the connection manager only looped every 30s.

The fix, and what these tests prove:
  * ONE shared predicate -- Peers.is_isolated() over the AUTHORITATIVE counter len(connection_pool)
    (the list, via outbound_count()) -- drives BOTH halves of recovery.
  * At/below the floor, client_loop wipes all back-off (reset_tried(aggressive=True)) and EVERY known
    peer becomes re-dial-eligible in that one pass.
  * The REAL ConnectionManager loop selects the 5s recovery cadence on that SAME predicate (coupled), so
    faster dialing always carries the redial-all benefit.
  * Above the floor, the back-off is respected and NO aggressive reset happens.

WHY THIS IS A *LOGIC/INTEGRATION* TEST, NOT A LIVE 2-NODE DIAL (honest limits):
A real two-node dial test is structurally impossible here: regnet nodes never dial out
(ConnectionManager skips client_loop entirely for regnet -- see connection_manager()), and these tests
run with no sockets, no disk, and no second process. So they drive the decision logic directly: a real
Peers instance with the disk/network maintenance helpers stubbed, a recording stand-in for the worker
(so we capture which peers WOULD be dialed without opening a socket), and the REAL production cadence
method ConnectionManager._loop_seconds() (the exact method the loop calls each pass). They prove the
back-off-bypass and the cadence fire together on the one shared counter; they do NOT prove a TCP
connection is actually established.

REGRESSION GUARD (the bug the reverted attempt hit): test_fix_uses_authoritative_list_not_set_counter
reproduces the exact divergence -- list count <= floor (isolated) while the _connection_pool_set count
is ABOVE the floor -- and asserts the aggressive reset STILL fires. The reverted code keyed the reset
off _connection_pool_set, so it would NOT fire here (faster cadence with no redial-all = the live
regression). This test fails on that code and passes on the fix.

Run with: python3 -m pytest tests/test_isolation_recovery.py -v
"""
import threading
import types
from time import time as _now

import connectionmanager
import peershandler
from peershandler import Peers, OUTBOUND_RECOVERY_FLOOR


class _Log:
    def info(self, *a):
        pass

    def warning(self, *a):
        pass


class _TestPeers(Peers):
    # Peers declares __slots__, so instances are not monkeypatchable; a subclass WITHOUT __slots__ gets a
    # __dict__, letting us override only the disk/network maintenance helpers. The dial-decision path
    # under test (client_loop, is_isolated, outbound_count, reset_tried, _order_by_recency) is fully
    # inherited and exercised unmodified.
    def peers_get(self, *a, **k):
        return {}

    def peers_test(self, *a, **k):
        return None


def _make_peers(known_peers, ip_to_mainnet=None):
    """A real Peers (dial logic intact) with a minimal config and no disk/socket side effects."""
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


def _run_client_loop_recording(p):
    """Run client_loop with a worker stand-in that records (host, port) instead of opening a socket.
    Returns the sorted list of peers that were dialed this pass."""
    dialed = []

    def fake_worker(host, port, node):
        dialed.append((host, port))

    p.client_loop(p.node, this_target=fake_worker)
    # The dial threads are daemons running the trivial recorder; let them finish.
    for t in threading.enumerate():
        if t.name.startswith("out_"):
            t.join(timeout=2)
    return sorted(dialed)


# ---------------------------------------------------------------------------------------------------
# (a) At/below the floor: aggressive reset fires AND every known peer is re-dial-eligible in one pass.
# ---------------------------------------------------------------------------------------------------

def test_isolated_node_aggressively_resets_and_redials_all_known_peers_in_one_pass():
    peers = {f"10.0.0.{i}": 5658 for i in range(1, 6)}  # 5 known peers
    p = _make_peers(peers)
    # Reproduce the stall: every known peer is deep in escalating back-off (5 min out), 0 outbound.
    p.tried = {f"10.0.0.{i}:5658": (3, _now() + 300) for i in range(1, 6)}
    assert p.outbound_count() == 0
    assert p.is_isolated() is True

    # Spy on reset_tried so we can assert it was called WITH aggressive=True (the bypass), not the
    # gentle window reset. (The instance has a __dict__, so this override shadows the inherited method.)
    reset_calls = []
    real_reset = p.reset_tried

    def spy_reset(aggressive=False):
        reset_calls.append(aggressive)
        return real_reset(aggressive=aggressive)
    p.reset_tried = spy_reset

    dialed = _run_client_loop_recording(p)

    # (a1) The aggressive reset MUST have been invoked (and at least once with aggressive=True).
    assert True in reset_calls, f"aggressive reset not invoked; reset_tried calls={reset_calls}"
    # (a2) Every known peer was re-dialed THIS pass despite each being 5 minutes into back-off.
    assert dialed == sorted((ip, 5658) for ip in peers), dialed
    # (a3) Back-off was wiped then re-armed by add_try -> every peer back to first try (tries==1).
    assert p.tried and all(v[0] == 1 for v in p.tried.values()), p.tried


def test_floor_boundary_is_inclusive_isolated_at_floor_not_above():
    # is_isolated() must be TRUE at exactly the floor and FALSE one above -- the boundary both the dial
    # bypass and the cadence pivot on, so it must be exact.
    peers = {"10.0.0.1": 5658}
    p = _make_peers(peers)
    p.connection_pool = [f"1.1.1.{i}:5658" for i in range(OUTBOUND_RECOVERY_FLOOR)]  # == floor
    assert p.outbound_count() == OUTBOUND_RECOVERY_FLOOR
    assert p.is_isolated() is True
    p.connection_pool.append("1.1.1.250:5658")                                       # floor + 1
    assert p.is_isolated() is False


# ---------------------------------------------------------------------------------------------------
# (b) The REAL ConnectionManager loop selects the 5s cadence on the SAME predicate (coupled).
# ---------------------------------------------------------------------------------------------------

class _CadencePeers:
    """A peers stand-in whose is_isolated()/outbound_count() are the EXACT production methods bound to a
    given connection_pool length, so the cadence test exercises the real coupling, not a copy."""
    def __init__(self, outbound):
        self.connection_pool = [f"9.9.9.{i}:5658" for i in range(outbound)]

    outbound_count = peershandler.Peers.outbound_count
    is_isolated = peershandler.Peers.is_isolated


def _cadence_chosen_by_real_code(outbound, is_regnet=False):
    """The cadence the REAL production code selects, via ConnectionManager._loop_seconds() -- the actual
    method the loop calls each pass. No re-implementation of the decision on the test side, and (unlike
    driving the whole connection_manager() loop) no infinite-loop / heavyweight-fake risk: we call the
    one decision method directly. It reads ONLY node.is_regnet and node.peers.is_isolated()."""
    node = types.SimpleNamespace(
        is_regnet=is_regnet, peers=_CadencePeers(outbound), IS_STOPPING=False,
    )
    cm = connectionmanager.ConnectionManager(node, mp=None)
    return cm._loop_seconds()


def test_connection_manager_uses_recovery_cadence_when_isolated():
    # outbound == 0 -> isolated -> the real loop must pick the 5s recovery cadence.
    assert _cadence_chosen_by_real_code(outbound=0) == connectionmanager.RECOVERY_LOOP_SECONDS


def test_connection_manager_uses_normal_cadence_when_well_connected():
    # outbound well above the floor -> not isolated -> the real loop must keep the 30s normal cadence.
    assert _cadence_chosen_by_real_code(outbound=OUTBOUND_RECOVERY_FLOOR + 5) \
        == connectionmanager.NORMAL_LOOP_SECONDS


def test_connection_manager_never_busyspins_on_regnet():
    # Regnet never dials, so even at 0 outbound it must use the full normal cadence (no 5s busy-spin).
    assert _cadence_chosen_by_real_code(outbound=0, is_regnet=True) \
        == connectionmanager.NORMAL_LOOP_SECONDS


def test_cadence_and_bypass_flip_on_the_same_boundary():
    # COUPLING, end to end: as the authoritative outbound count crosses the floor, the cadence chosen by
    # the REAL ConnectionManager loop and the is_isolated() predicate that gates the client_loop bypass
    # must flip TOGETHER -- never one without the other.
    at_floor = OUTBOUND_RECOVERY_FLOOR
    above = OUTBOUND_RECOVERY_FLOOR + 1

    # at the floor: isolated AND recovery cadence
    p_at = _make_peers({"10.0.0.1": 5658})
    p_at.connection_pool = [f"1.1.1.{i}:5658" for i in range(at_floor)]
    assert p_at.is_isolated() is True
    assert _cadence_chosen_by_real_code(outbound=at_floor) == connectionmanager.RECOVERY_LOOP_SECONDS

    # one above: NOT isolated AND normal cadence
    p_above = _make_peers({"10.0.0.1": 5658})
    p_above.connection_pool = [f"1.1.1.{i}:5658" for i in range(above)]
    assert p_above.is_isolated() is False
    assert _cadence_chosen_by_real_code(outbound=above) == connectionmanager.NORMAL_LOOP_SECONDS


# ---------------------------------------------------------------------------------------------------
# (c) Above the floor: back-off respected, NO aggressive reset.
# ---------------------------------------------------------------------------------------------------

def test_well_connected_node_respects_backoff_and_does_not_bypass():
    peers = {f"10.0.0.{i}": 5658 for i in range(1, 6)}
    p = _make_peers(peers)
    # Above the floor: pretend we already hold plenty of outbound connections (authoritative list).
    p.connection_pool = [f"192.168.0.{i}:5658" for i in range(OUTBOUND_RECOVERY_FLOOR + 3)]
    assert p.is_isolated() is False
    # All known peers are cooling down; the steady state must RESPECT that (no aggressive bypass).
    p.tried = {f"10.0.0.{i}:5658": (2, _now() + 300) for i in range(1, 6)}

    reset_calls = []
    real_reset = p.reset_tried

    def spy_reset(aggressive=False):
        reset_calls.append(aggressive)
        return real_reset(aggressive=aggressive)
    p.reset_tried = spy_reset

    dialed = _run_client_loop_recording(p)

    # NOT isolated -> back-off honored -> none of the cooling-down peers dialed.
    assert dialed == [], dialed
    # An aggressive reset must NEVER have happened above the floor (gentle reset_tried() may run, but it
    # is only ever called with aggressive=False).
    assert True not in reset_calls, f"aggressive reset fired above the floor: {reset_calls}"
    # Their back-off timers are untouched (still tries==2, still in the future).
    assert all(v[0] == 2 for v in p.tried.values()), p.tried


def test_recovery_prioritizes_recently_responsive_peers():
    # ip_to_mainnet holds peers we have completed a handshake with => recently responsive; on the
    # recovery path those are dialed first so we reconnect to a live peer ASAP.
    peers = {f"10.0.0.{i}": 5658 for i in range(1, 7)}
    proven_ips = {"10.0.0.2": "mainnet", "10.0.0.5": "mainnet"}
    p = _make_peers(peers, ip_to_mainnet=proven_ips)
    ordered = list(p._order_by_recency(peers).keys())
    assert set(ordered[:2]) == set(proven_ips), ordered          # proven peers first
    assert set(ordered) == set(peers), "ordering must not drop or add peers"


# ---------------------------------------------------------------------------------------------------
# REGRESSION GUARD: the exact divergence that broke the reverted attempt.
# ---------------------------------------------------------------------------------------------------

def test_fix_uses_authoritative_list_not_set_counter():
    # The reverted attempt gated the aggressive reset on len(_connection_pool_set) but the cadence on
    # len(connection_pool); under churn the set can be LARGER than the list (a duplicate append inflates
    # the list but the set dedupes; a removal shrinks the set only once), so on the live node the
    # cadence sped up while the aggressive reset NEVER fired -- faster dialing, no redial-all, hammering
    # the few reachable peers. Here we recreate that divergence and assert the FIX still fires the
    # aggressive reset (because is_isolated() reads the LIST). On the reverted code this test fails.
    peers = {f"10.0.0.{i}": 5658 for i in range(1, 6)}
    p = _make_peers(peers)
    # Authoritative LIST says isolated (<= floor); the optimization SET is stale/inflated ABOVE the floor.
    p.connection_pool = []                                                   # outbound_count() == 0
    p._connection_pool_set = {f"7.7.7.{i}:5658" for i in range(OUTBOUND_RECOVERY_FLOOR + 4)}
    assert p.outbound_count() == 0
    assert len(p._connection_pool_set) > OUTBOUND_RECOVERY_FLOOR             # the divergence
    assert p.is_isolated() is True, "is_isolated must read the authoritative LIST, not the set"

    p.tried = {f"10.0.0.{i}:5658": (3, _now() + 300) for i in range(1, 6)}
    reset_calls = []
    real_reset = p.reset_tried

    def spy_reset(aggressive=False):
        reset_calls.append(aggressive)
        return real_reset(aggressive=aggressive)
    p.reset_tried = spy_reset

    dialed = _run_client_loop_recording(p)

    assert True in reset_calls, (
        "aggressive reset did NOT fire while the LIST said isolated -- this is the reverted-attempt "
        f"regression (reset gated on the set, not the list). reset_tried calls={reset_calls}")
    assert dialed == sorted((ip, 5658) for ip in peers), dialed


def test_floor_and_cadence_constants_are_sane_and_single_sourced():
    # The recovery path must trigger only when genuinely low, the recovery cadence must be much tighter
    # than the normal one (else "seconds not minutes" fails), and connectionmanager must reuse the SAME
    # cadence constants peershandler defines (one source of truth, so they can't drift apart).
    assert 1 <= OUTBOUND_RECOVERY_FLOOR <= 8
    assert connectionmanager.NORMAL_LOOP_SECONDS is peershandler.NORMAL_LOOP_SECONDS
    assert connectionmanager.RECOVERY_LOOP_SECONDS is peershandler.RECOVERY_LOOP_SECONDS
    assert connectionmanager.RECOVERY_LOOP_SECONDS < connectionmanager.NORMAL_LOOP_SECONDS
    assert connectionmanager.RECOVERY_LOOP_SECONDS >= 1  # still polite, not a busy-spin
