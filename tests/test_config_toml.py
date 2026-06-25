"""TOML config loader (options.Get.load_toml, doc/11) — proves the modern config.toml is byte-identical
to the equivalent legacy config.txt, and that read()'s format preference, the config_custom override
precedence, and the BISMUTH_IGNORE_CONFIG_CUSTOM gate are unchanged.

Hermetic: no node spawn, no ledger access — pure loader tests in pytest tmp dirs, so the live config.txt
is never touched. Run: python3 -m pytest tests/test_config_toml.py -v
"""
import os

import pytest

import options

pytest.importorskip("tomllib")  # stdlib on Python 3.11+; the node targets 3.12

# config.txt covering one key of each coercion type (incl. the renamed `verify` and the str-typed `port`)
TXT = (
    "port=5658\n"
    "rest_api_port=5659\n"
    "verify=false\n"
    "regnet=False\n"
    "version_allow=mainnet0024,mainnet0025\n"
    'light_ip={"127.0.0.1": "5658"}\n'
)

# the EQUIVALENT config.toml — native types; `port` written as an int to exercise the str-coercion edge
TOML = (
    "port = 5658\n"
    "rest_api_port = 5659\n"
    "verify = false\n"
    "regnet = false\n"
    'version_allow = ["mainnet0024", "mainnet0025"]\n'
    'light_ip = { "127.0.0.1" = "5658" }\n'
)


def _load_txt(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text(TXT)
    g = options.Get()
    g.load_file(str(p))
    return g


def _load_toml(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(TOML)
    g = options.Get()
    g.load_toml(str(p))
    return g


def test_toml_equals_txt(tmp_path):
    """The decisive proof: a config.toml and the equivalent config.txt produce an identical Config."""
    a = _load_txt(tmp_path).__dict__
    b = _load_toml(tmp_path).__dict__
    assert a == b, "config.toml did not load to the same Config object as the equivalent config.txt"


def test_toml_coercion_edges(tmp_path):
    g = _load_toml(tmp_path)
    assert g.port == "5658" and isinstance(g.port, str)          # int in TOML -> str (schema type is str)
    assert g.rest_api_port == 5659 and isinstance(g.rest_api_port, int)
    assert g.verify is False                                      # renamed key (verify->verify), native bool
    assert g.version_allow == ["mainnet0024", "mainnet0025"]      # comma-list -> TOML array
    assert g.light_ip == {"127.0.0.1": "5658"}                    # JSON-dict -> TOML inline table


def test_unknown_key_skipped(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('port = "5658"\nbogus_unknown_key = 1\n')
    g = options.Get()
    g.load_toml(str(p))
    assert not hasattr(g, "bogus_unknown_key")          # silent-skip-unknown preserved


def test_read_prefers_toml_then_falls_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BISMUTH_IGNORE_CONFIG_CUSTOM", "1")   # isolate the base layer
    (tmp_path / "config.txt").write_text("port=1111\n")
    (tmp_path / "config.toml").write_text('port = "2222"\n')
    g = options.Get()
    g.read()
    assert g.port == "2222"                              # config.toml wins when present
    os.remove(tmp_path / "config.toml")
    g2 = options.Get()
    g2.read()
    assert g2.port == "1111"                             # falls back to legacy config.txt


def test_custom_override_precedence_and_ignore(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BISMUTH_IGNORE_CONFIG_CUSTOM", raising=False)
    (tmp_path / "config.txt").write_text("port=1111\nrest_api=False\n")
    (tmp_path / "config_custom.toml").write_text("rest_api = true\n")
    g = options.Get()
    g.read()
    assert g.port == "1111"          # base key untouched by the override
    assert g.rest_api is True        # config_custom.toml override applied over the base
    monkeypatch.setenv("BISMUTH_IGNORE_CONFIG_CUSTOM", "1")
    g2 = options.Get()
    g2.read()
    assert g2.rest_api is False       # IGNORE env skips the custom layer entirely


def test_migrate_tool_roundtrips(tmp_path):
    """scripts/migrate_config.py emits TOML that loads to the same Config as its legacy source."""
    import importlib.util

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "migrate_config", os.path.join(root, "scripts", "migrate_config.py"))
    mc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mc)

    src = tmp_path / "config.txt"
    src.write_text(TXT)
    (tmp_path / "config.toml").write_text(mc.build_toml(str(src)))
    ok, a, b = mc._config_equal(str(src), str(tmp_path / "config.toml"))
    assert ok, "migrate tool output did not round-trip to an identical Config"
    # the real shipped template must also round-trip
    ok2, _, _ = mc._config_equal(os.path.join(root, "config.txt.example"),
                                 os.path.join(root, "config.toml.example"))
    assert ok2, "config.toml.example is not equivalent to config.txt.example"
