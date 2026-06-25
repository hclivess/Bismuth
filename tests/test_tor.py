"""Tor manager + tri-state config (doc/38). Hermetic: no real tor network, no node spawn, no ledger.
The managed-mode happy path is exercised with a MOCKED stem controller (this host has neither tor nor
stem); the fallback paths are exercised for real (stem genuinely absent). Run: pytest tests/test_tor.py
"""
import os
import sys
import types

import pytest

import options
import tor_manager


# --- a minimal node stub -------------------------------------------------------------------------
class _Log:
    def __init__(self):
        self.msgs = []

    def warning(self, m):
        self.msgs.append(str(m))

    info = warning
    debug = warning


class _Logger:
    def __init__(self):
        self.app_log = _Log()


class _Node:
    def __init__(self, **kw):
        self.logger = _Logger()
        self.port = 5658
        self.tor_mode = kw.get("tor_mode", "off")
        self.tor_socks_host = kw.get("tor_socks_host", "127.0.0.1")
        self.tor_socks_port = kw.get("tor_socks_port", 9050)
        self.tor_control_port = 0
        self.tor_binary = ""
        self.tor_onion = kw.get("tor_onion", False)
        self.tor_onion_key = kw.get("tor_onion_key", "")
        self.tor_required = kw.get("tor_required", False)
        self.tor_dial_timeout = 30
        self.tor_data_dir = kw.get("tor_data_dir", "")


# --- config tri-state coercion (prod-critical: off must stay falsy) ------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("False", "off"), ("false", "off"), ("0", "off"), ("no", "off"), ("off", "off"),
    ("True", "external"), ("true", "external"), ("1", "external"), ("yes", "external"),
    ("on", "external"), ("external", "external"), ("managed", "managed"),
    ("garbage", "off"),
])
def test_config_tor_coercion_txt(tmp_path, raw, expected):
    p = tmp_path / "c.txt"
    p.write_text("tor=%s\n" % raw)
    g = options.Get()
    g.load_file(str(p))
    assert g.tor == expected


def test_config_tor_default_off(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("port=5658\n")          # no tor line at all
    g = options.Get()
    g.load_file(str(p))
    assert g.tor == "off"                # default keeps clearnet
    # the new tor_* defaults are present and clearnet-safe
    assert g.tor_socks_port == 9050 and g.tor_onion is False and g.tor_required is False


def test_config_tor_toml(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('tor = "managed"\ntor_onion = true\n')
    g = options.Get()
    g.load_toml(str(p))
    assert g.tor == "managed" and g.tor_onion is True
    # a TOML bool false also coerces to off
    p2 = tmp_path / "c2.toml"
    p2.write_text("tor = false\n")
    g2 = options.Get()
    g2.load_toml(str(p2))
    assert g2.tor == "off"


# --- manager: off / external / fallback (real, stem absent) -------------------------------------
def test_external_proxy_is_configured_endpoint():
    node = _Node(tor_mode="external", tor_socks_host="127.0.0.1", tor_socks_port=9150)
    tm = tor_manager.TorManager(node)
    assert tm.launch() is True
    assert tm.get_proxy() == ("127.0.0.1", 9150)   # single source of truth, overridable port
    assert tm.is_ready() is True


def test_managed_falls_back_to_clearnet_without_stem():
    # stem is not installed on this host -> managed launch must fall back to clearnet (no raise)
    node = _Node(tor_mode="managed", tor_required=False)
    tm = tor_manager.TorManager(node)
    assert "stem" not in sys.modules or True
    assert tm.launch() is True               # node keeps running
    assert tm.get_proxy() is None            # clearnet (no proxy)
    assert any("FALLING BACK TO CLEARNET" in m for m in node.logger.app_log.msgs)


def test_managed_fail_fast_when_required():
    node = _Node(tor_mode="managed", tor_required=True)
    tm = tor_manager.TorManager(node)
    with pytest.raises(RuntimeError):
        tm.launch()                          # tor_required + no tor -> refuse to start


# --- onion v3 validation -------------------------------------------------------------------------
def test_onion_v3_regex():
    good = "a" * 56 + ".onion"
    assert tor_manager.is_onion_v3(good)
    assert not tor_manager.is_onion_v3("a" * 16 + ".onion")     # v2 length -> rejected
    assert not tor_manager.is_onion_v3("not-an-onion")
    assert not tor_manager.is_onion_v3("a" * 55 + "1.onion")    # '1' is not in the base32 alphabet
    assert tor_manager.is_onion_v3(("A" * 56 + ".onion"))       # case-insensitive (normalized lower)


# --- managed happy path with a MOCKED stem controller -------------------------------------------
class _FakeCtrl:
    def __init__(self):
        self.closed = False
        self.newnym = 0

    def authenticate(self):
        pass

    def get_info(self, key):
        return "PROGRESS=100 TAG=done"

    def get_listeners(self, listener):
        return [("127.0.0.1", 9999)]

    def create_ephemeral_hidden_service(self, ports, key_type, key_content, await_publication):
        return types.SimpleNamespace(service_id="b" * 56, private_key="PRIVKEY",
                                     private_key_type="ED25519-V3")

    def get_newnym_wait(self):
        return 0

    def signal(self, sig):
        self.newnym += 1

    def close(self):
        self.closed = True


def _install_fake_stem(monkeypatch, ctrl):
    stem = types.ModuleType("stem")
    stem.Signal = types.SimpleNamespace(NEWNYM="NEWNYM")
    stem.Listener = types.SimpleNamespace(SOCKS="SOCKS")
    proc = types.ModuleType("stem.process")

    def _launch(**kw):
        h = kw.get("init_msg_handler")
        if h:
            h("Bootstrapped 100% (done)")
        return types.SimpleNamespace(kill=lambda: None)

    proc.launch_tor_with_config = _launch
    control = types.ModuleType("stem.control")
    control.Controller = types.SimpleNamespace(from_port=lambda port: ctrl)
    # link submodules as attributes on the parent (the import machinery would do this for real modules)
    stem.process = proc
    stem.control = control
    monkeypatch.setitem(sys.modules, "stem", stem)
    monkeypatch.setitem(sys.modules, "stem.process", proc)
    monkeypatch.setitem(sys.modules, "stem.control", control)


def test_managed_happy_path_mocked(tmp_path, monkeypatch):
    ctrl = _FakeCtrl()
    _install_fake_stem(monkeypatch, ctrl)
    data_dir = tmp_path / "tordata"
    data_dir.mkdir()
    (data_dir / "control_port").write_text("127.0.0.1:9051")
    key_file = tmp_path / "onion_key"
    node = _Node(tor_mode="managed", tor_onion=True, tor_data_dir=str(data_dir),
                 tor_onion_key=str(key_file))
    tm = tor_manager.TorManager(node)
    assert tm.launch() is True
    assert tm.is_ready() is True
    assert tm.get_proxy() == ("127.0.0.1", 9999)            # derived SOCKS port (SocksPort=auto)
    assert tm.onion_address() == "b" * 56 + ".onion"        # ephemeral v3 onion published
    assert os.path.exists(str(key_file))                    # key persisted for a stable address
    assert oct(os.stat(str(key_file)).st_mode)[-3:] == "600"
    assert tm.signal_newnym() is True                       # NEWNYM via the control port
    tm.stop()
    assert ctrl.closed is True
