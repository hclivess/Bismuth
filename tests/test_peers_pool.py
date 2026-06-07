"""
Peer connection-pool back-off tests (PeersPoolMixin).

Locks in the catch-up-stall fix: the retry back-off is gentle and capped (30s, 1m, 2m, then 5m), and
reset_tried() — called when the node is connection-starved — frees peers whose back-off is more than a
short window out, so a catching-up node re-attempts live peers within ~a minute instead of waiting out
a 5-30 minute cooldown. Pure unit tests: no node, no sockets.

Run with: python3 -m pytest tests/test_peers_pool.py -v
"""
import types
from time import time

from peers_pool import PeersPoolMixin


class _Log:
    def info(self, *a):
        pass

    def warning(self, *a):
        pass


class _PeersStub(PeersPoolMixin):
    # PeersPoolMixin declares __slots__=(); a subclass without __slots__ gets a __dict__, so we can
    # attach exactly the attributes the pool methods touch.
    def __init__(self):
        self.tried = {}
        self.banlist = []
        self.whitelist = []
        self._connection_pool_set = set()
        self._c_class_cache = {}
        self.app_log = _Log()
        self.config = types.SimpleNamespace(port="5658", node_ip="203.0.113.7")

    def is_whitelisted(self, host):
        return host in self.whitelist


def test_add_try_backoff_schedule_is_gentle_and_capped():
    p = _PeersStub()
    host, port = "1.2.3.4", 5658
    hp = f"{host}:{port}"
    expected = [30, 60, 120, 300, 300]  # capped at 5 min, not 30 min
    for want in expected:
        before = time()
        p.add_try(host, port)
        tries, when = p.tried[hp]
        assert abs((when - before) - want) < 5, f"expected ~{want}s back-off, got {when - before:.0f}s"
    assert p.tried[hp][0] == 3  # tries saturate at 3


def test_reset_tried_frees_long_backoffs_keeps_imminent():
    p = _PeersStub()
    now = time()
    p.tried = {
        "soon:5658": (1, now + 20),       # cooling down for 20s -> keep
        "fivemin:5658": (2, now + 300),   # 5 min out -> free it (this was the stall)
        "fifteen:5658": (3, now + 900),   # 15 min out -> free it
    }
    p.reset_tried()
    assert "soon:5658" in p.tried
    assert "fivemin:5658" not in p.tried
    assert "fifteen:5658" not in p.tried


def test_can_connect_to_respects_active_cooldown_then_clears():
    p = _PeersStub()
    host, port = "9.9.9.9", 5658
    hp = f"{host}:{port}"
    p.tried[hp] = (1, time() + 120)        # cooling down
    assert p.can_connect_to(host, port) is False
    p.tried[hp] = (1, time() - 1)          # cooldown expired
    assert p.can_connect_to(host, port) is True
    p.banlist.append(host)                 # banned overrides
    assert p.can_connect_to(host, port) is False


def test_can_connect_to_refuses_dialing_self():
    p = _PeersStub()
    p.whitelist = ["127.0.0.1"]  # even whitelisted, we must not dial ourselves
    # our own listening address, via loopback and via our advertised node_ip, on our own port
    assert p.can_connect_to("127.0.0.1", 5658) is False
    assert p.can_connect_to("127.0.0.1", "5658") is False
    assert p.can_connect_to("203.0.113.7", 5658) is False
    assert p.can_connect_to("localhost", 5658) is False
    # a different local service (different port) is still allowed
    assert p.can_connect_to("127.0.0.1", 9999) is True
    # a real remote peer is allowed
    assert p.can_connect_to("185.184.192.210", 5658) is True


def test_del_try_removes_entry_and_is_safe_on_missing():
    p = _PeersStub()
    p.tried["1.2.3.4:5658"] = (2, time() + 999)
    p.del_try("1.2.3.4", 5658)
    assert "1.2.3.4:5658" not in p.tried
    p.del_try("9.9.9.9", 5658)             # missing peer -> no raise (pop-with-default)
    p.tried["5.6.7.8:5658"] = (1, time() + 1)
    p.del_try("5.6.7.8:5658")              # also accepts a prebuilt "ip:port"
    assert "5.6.7.8:5658" not in p.tried
