"""
Bug-fix audit tests (this pass: never-before-audited peer / sync / mining modules).

HERMETIC: pure unit tests — no node subprocess, no real sockets, no disk. Each test builds a minimal
stand-in object for the mixin under test (the mixin declares ``__slots__=()``; a plain subclass gets a
``__dict__`` so we can attach exactly the attributes the method touches) and monkeypatches the module's
``socks`` so the "probe" socket is a fake we can inspect.

CONFIRMED FIX under test:
  * peers_storage.PeersStorageMixin.peersync — the probe socket (``s_purge``) was closed only on the
    SUCCESS path; when ``connect()`` raised (the normal unreachable-peer case, which is exactly when
    peersync runs against every freshly-announced peer) the socket was never closed, leaking a file
    descriptor on every cycle. Fixed by closing it in a ``finally`` (mirroring ``peers_test``).

Run with: python3 -m pytest tests/test_bugfix_audit.py -v
"""
import threading
import types

import peers_storage
from peers_storage import PeersStorageMixin


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _FakeSocket:
    """Stands in for ``socks.socksocket()``. Records close() calls; ``connect`` raises by default to
    reproduce the unreachable-peer path that leaked the fd."""

    def __init__(self, connect_raises=True):
        self._connect_raises = connect_raises
        self.closed = False
        self.close_count = 0

    def settimeout(self, *_a):
        pass

    def setproxy(self, *_a, **_k):
        pass

    def connect(self, *_a):
        if self._connect_raises:
            raise OSError("Network is unreachable")

    def close(self):
        self.closed = True
        self.close_count += 1


class _PeersStub(PeersStorageMixin):
    def __init__(self, accept_peers=True, tor=False):
        self.app_log = _Log()
        self.peer_dict = {}
        self.peersync_lock = threading.Lock()
        self.config = types.SimpleNamespace(accept_peers=accept_peers, tor=tor)


def _install_fake_socket(monkeypatch, connect_raises):
    """Make ``peers_storage.socks.socksocket()`` return a single shared _FakeSocket we can inspect."""
    made = []

    def factory():
        sock = _FakeSocket(connect_raises=connect_raises)
        made.append(sock)
        return sock

    fake_socks = types.SimpleNamespace(socksocket=factory, PROXY_TYPE_SOCKS5=2)
    monkeypatch.setattr(peers_storage, "socks", fake_socks)
    return made


def test_peersync_closes_probe_socket_when_connect_fails(monkeypatch):
    """The fd-leak regression: an unreachable new peer must still get its probe socket closed."""
    made = _install_fake_socket(monkeypatch, connect_raises=True)
    p = _PeersStub()

    added = p.peersync('{"10.0.0.1": "5658"}')

    assert made, "peersync never created a probe socket"
    assert made[0].closed is True, "probe socket leaked: connect() failed but close() was never called"
    # an unreachable peer is not saved, and nothing was added
    assert added == 0
    assert "10.0.0.1" not in p.peer_dict


def test_peersync_closes_probe_socket_on_success_and_saves_peer(monkeypatch):
    """Success path still closes the socket exactly once and records the reachable peer (no double close,
    no behavioural regression from the try/finally wrap)."""
    made = _install_fake_socket(monkeypatch, connect_raises=False)
    p = _PeersStub()

    added = p.peersync('{"10.0.0.2": "5658"}')

    assert made[0].closed is True
    assert made[0].close_count == 1, "socket closed more than once on the success path"
    assert added == 1
    assert p.peer_dict.get("10.0.0.2") == "5658"


def test_peersync_does_not_leak_across_many_unreachable_peers(monkeypatch):
    """Every probe socket across a batch of unreachable peers is closed — i.e. no accumulation."""
    made = _install_fake_socket(monkeypatch, connect_raises=True)
    p = _PeersStub()

    peers = {f"10.0.1.{i}": "5658" for i in range(1, 6)}
    import json
    p.peersync(json.dumps(peers))

    assert len(made) == len(peers), "expected one probe socket per new peer"
    assert all(s.closed for s in made), "at least one probe socket leaked across the batch"


def test_peersync_refuses_when_not_accepting_peers(monkeypatch):
    """Guard clause unchanged: returns -1 and never touches a socket when accept_peers is False."""
    made = _install_fake_socket(monkeypatch, connect_raises=True)
    p = _PeersStub(accept_peers=False)

    assert p.peersync('{"10.0.0.9": "5658"}') == -1
    assert made == [], "no probe socket should be created when not accepting peers"
