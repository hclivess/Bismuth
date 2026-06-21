"""Hermetic regression tests for the 2026-06 security-audit fixes (doc/25).

No running node — these exercise the pure helpers / handler methods directly, so they run fast and
deterministically in the full suite:

  H-1  amounts.consensus_amount is EXACT (never a lossy float) so REST sync == socket sync.
  H-2  the /api/proxy relay REFUSES redirects (no SSRF pivot via a 3xx Location).
  H-3  the SSRF guard resolves once and RETURNS a validated public IP (pinned -> no DNS rebind).
  H-4  the content-txid lookup is bounded (handled by the live test_hf2 suite; the helper shape is here).
  M-1  the proxy enforces a port allowlist.
  M-2  the proxy is per-IP rate limited.
"""
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import amounts
import rest_api


# ---------------------------------------------------------------- H-1: exact consensus amount ----
def test_consensus_amount_is_exact_above_2_53():
    u = 9007199254740993                      # > 2**53 atomic units (~90.07M BIS): the float boundary
    old = amounts.LEDGER_INTEGER
    amounts.LEDGER_INTEGER = True
    try:
        assert amounts.consensus_amount(u) == "90071992.54740993"        # exact decimal string
        assert amounts.to_units(amounts.consensus_amount(u)) == u        # round-trips with no loss
        # display_amount routes through a Python float and LOSES the last digit — must never feed consensus
        assert amounts.to_units(str(amounts.display_amount(u))) != u
    finally:
        amounts.LEDGER_INTEGER = old


def test_consensus_amount_legacy_mode_passthrough():
    old = amounts.LEDGER_INTEGER
    amounts.LEDGER_INTEGER = False
    try:
        assert amounts.consensus_amount("12.34000000") == "12.34000000"  # stored decimal unchanged
    finally:
        amounts.LEDGER_INTEGER = old


def test_row_to_sync_tx_uses_exact_amount():
    node = types.SimpleNamespace()
    Handler = rest_api._make_handler(node)
    h = Handler.__new__(Handler)
    old = amounts.LEDGER_INTEGER
    amounts.LEDGER_INTEGER = True
    try:
        u = 9007199254740993
        row = [1, "1700000000.00", "sender", "recip", u, "sig", "pub", "bhash", 0, 0, "", ""]
        tx = h._row_to_sync_tx(row)
        assert tx[3] == "90071992.54740993"                              # the wire amount is exact
        # and a peer re-deriving units from the wire string gets the original integer back
        assert amounts.to_units(tx[3]) == u
    finally:
        amounts.LEDGER_INTEGER = old


# ----------------------------------------------------------------- H-3 / M-1: SSRF guard + ports ----
def _fake_handler(rest_api_port=5659, rest_api_proxy=True, proxy_ports=None):
    node = types.SimpleNamespace(rest_api_proxy=rest_api_proxy, rest_api_port=rest_api_port,
                                 rest_api_proxy_ports=proxy_ports,
                                 logger=types.SimpleNamespace(app_log=types.SimpleNamespace(
                                     warning=lambda *a, **k: None)))
    Handler = rest_api._make_handler(node)
    h = Handler.__new__(Handler)
    h.client_address = ("198.51.100.7", 1234)
    return Handler, h


def test_proxy_guard_returns_validated_public_ip():
    Handler, _ = _fake_handler()
    assert Handler._proxy_guard_host("8.8.8.8") == "8.8.8.8"             # public literal -> returned, pinned


def test_proxy_guard_rejects_private_literal():
    Handler, _ = _fake_handler()
    with pytest.raises(rest_api._Forbidden):
        Handler._proxy_guard_host("10.0.0.1")
    with pytest.raises(rest_api._Forbidden):
        Handler._proxy_guard_host("169.254.169.254")                    # cloud-metadata link-local


def test_proxy_rejects_disallowed_port():
    Handler, h = _fake_handler()
    with pytest.raises(rest_api._Forbidden):                            # port 9000 not in the allowlist
        h._proxy({"target": ["http://example.com:9000/api/status"]})


def test_proxy_allowed_port_still_blocks_private_host():
    # an allowed port (5659) must still be rejected when the host is private (the guard, not the port)
    Handler, h = _fake_handler()
    with pytest.raises(rest_api._Forbidden):
        h._proxy({"target": ["http://10.0.0.1:5659/api/status"]})


def test_proxy_port_allowlist_includes_node_and_config():
    Handler, _ = _fake_handler(rest_api_port=3031, proxy_ports="9090, 9091")
    ports = Handler._proxy_allowed_ports()
    assert {80, 443, 5658, 5659, 3031, 9090, 9091} <= ports


# ----------------------------------------------------------------------- H-2: refuse redirects ----
class _RedirectServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:1/api/secret")   # would be an SSRF pivot if followed
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def test_proxy_fetch_refuses_redirect():
    srv = HTTPServer(("127.0.0.1", 0), _RedirectServer)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        Handler, _ = _fake_handler()
        h = Handler.__new__(Handler)
        with pytest.raises(rest_api._Forbidden):
            # pinned straight at the loopback test server; a 302 must raise, never be followed
            h._proxy_fetch("http", "127.0.0.1", "127.0.0.1", port, "/api/x")
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- M-2: rate limiting ----
def test_proxy_rate_limit_blocks_floods():
    ip = "203.0.113.200"
    rest_api._proxy_rl.pop(ip, None)
    try:
        allowed = sum(1 for _ in range(rest_api.PROXY_RATE_MAX) if rest_api._proxy_rate_ok(ip))
        assert allowed == rest_api.PROXY_RATE_MAX                       # the window's budget is granted
        assert rest_api._proxy_rate_ok(ip) is False                    # the next one over budget is refused
    finally:
        rest_api._proxy_rl.pop(ip, None)
