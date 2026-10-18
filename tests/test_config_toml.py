"""TOML configuration (options.Get, doc/11): config.toml is THE format.

Proves the TOML loader's typing, the config_custom.toml override precedence and the
BISMUTH_IGNORE_CONFIG_CUSTOM gate, and the one-time migration of a pre-TOML key=value config.txt: verified
to load to the identical Config, written atomically, the old file kept as config.txt.migrated and never
read again.

Hermetic: no node spawn, no ledger access — pure loader tests in pytest tmp dirs, so the live config is
never touched. Run: python3 -m pytest tests/test_config_toml.py -v
"""
import importlib.util
import os

import pytest

import options

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# a key=value config covering one key of each coercion type (incl. the renamed `verify`, the str `port`)
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


def _get(tmp_path, monkeypatch, ignore_custom=True):
    monkeypatch.chdir(tmp_path)
    if ignore_custom:
        monkeypatch.setenv("BISMUTH_IGNORE_CONFIG_CUSTOM", "1")
    else:
        monkeypatch.delenv("BISMUTH_IGNORE_CONFIG_CUSTOM", raising=False)
    g = options.Get()
    g.read()
    return g


def _loaded(loader, path):
    g = options.Get()
    getattr(g, loader)(str(path))
    return g.__dict__


def test_toml_equals_the_key_value_form(tmp_path):
    (tmp_path / "c.txt").write_text(TXT)
    (tmp_path / "c.toml").write_text(TOML)
    assert _loaded("load_file", tmp_path / "c.txt") == _loaded("load_toml", tmp_path / "c.toml")


def test_toml_coercion_edges(tmp_path):
    (tmp_path / "c.toml").write_text(TOML)
    g = options.Get()
    g.load_toml(str(tmp_path / "c.toml"))
    assert g.port == "5658" and isinstance(g.port, str)          # int in TOML -> str (schema type is str)
    assert g.rest_api_port == 5659 and isinstance(g.rest_api_port, int)
    assert g.verify is False
    assert g.version_allow == ["mainnet0024", "mainnet0025"]
    assert g.light_ip == {"127.0.0.1": "5658"}


def test_unknown_key_skipped(tmp_path):
    (tmp_path / "c.toml").write_text('port = "5658"\nbogus_unknown_key = 1\n')
    g = options.Get()
    g.load_toml(str(tmp_path / "c.toml"))
    assert not hasattr(g, "bogus_unknown_key")


def test_read_uses_config_toml(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('port = "2222"\n')
    assert _get(tmp_path, monkeypatch).port == "2222"


def test_read_ignores_a_txt_beside_an_existing_toml(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('port = "2222"\n')
    (tmp_path / "config.txt").write_text("port=1111\n")
    assert _get(tmp_path, monkeypatch).port == "2222"
    assert (tmp_path / "config.txt").exists()                    # not migrated over an existing toml


def test_read_migrates_a_legacy_config_once(tmp_path, monkeypatch):
    (tmp_path / "config.txt").write_text(TXT + "# a comment\nrest_api=True\n")
    g = _get(tmp_path, monkeypatch)
    assert g.port == "5658" and g.rest_api is True and g.version_allow == ["mainnet0024", "mainnet0025"]
    assert not (tmp_path / "config.txt").exists()
    assert (tmp_path / "config.txt.migrated").exists()
    toml_text = (tmp_path / "config.toml").read_text()
    assert 'port = "5658"' in toml_text and "rest_api = true" in toml_text
    assert "debug_level" not in toml_text                        # only the keys the operator set
    # the migrated file loads to exactly what the old one did
    ref = options.Get()
    ref.load_file(str(tmp_path / "config.txt.migrated"))
    assert g.__dict__ == ref.__dict__
    # a second start reads the toml and leaves everything alone
    (tmp_path / "config.toml").write_text(toml_text.replace("rest_api_port = 5659", "rest_api_port = 6000"))
    assert _get(tmp_path, monkeypatch).rest_api_port == 6000


def test_failed_migration_writes_nothing(tmp_path, monkeypatch):
    (tmp_path / "config.txt").write_text(TXT)
    monkeypatch.setattr(options, "build_toml", lambda p: 'port = "9"\n')   # a lossy conversion
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        options.migrate_legacy("config.txt", "config.toml")
    assert (tmp_path / "config.txt").exists()
    assert not (tmp_path / "config.toml").exists() and not (tmp_path / "config.toml.tmp").exists()


def test_custom_override_precedence_and_ignore(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('port = "1111"\nrest_api = false\n')
    (tmp_path / "config_custom.toml").write_text("rest_api = true\n")
    g = _get(tmp_path, monkeypatch, ignore_custom=False)
    assert g.port == "1111" and g.rest_api is True
    assert _get(tmp_path, monkeypatch, ignore_custom=True).rest_api is False


def test_legacy_custom_is_migrated_unless_ignored(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('port = "1111"\n')
    (tmp_path / "config_custom.txt").write_text("rest_api=True\n")
    _get(tmp_path, monkeypatch, ignore_custom=True)
    assert (tmp_path / "config_custom.txt").exists()             # the mainnet service never touches it
    g = _get(tmp_path, monkeypatch, ignore_custom=False)
    assert g.rest_api is True
    assert (tmp_path / "config_custom.toml").exists() and (tmp_path / "config_custom.txt.migrated").exists()


def _migrate_tool():
    spec = importlib.util.spec_from_file_location("migrate_config", os.path.join(ROOT, "scripts", "migrate_config.py"))
    mc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mc)
    return mc


def test_migrate_tool(tmp_path, monkeypatch, capsys):
    mc = _migrate_tool()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.txt").write_text(TXT)
    assert mc.main(["--check"]) == 0
    assert (tmp_path / "config.txt").exists() and not (tmp_path / "config.toml").exists()   # check writes nothing
    assert mc.main([]) == 0
    assert (tmp_path / "config.toml").exists() and (tmp_path / "config.txt.migrated").exists()
    (tmp_path / "config.txt").write_text(TXT)
    assert mc.main([]) == 1                                      # refuses to overwrite without --force
    assert mc.main(["--force"]) == 0
    assert "OK (--check)" in capsys.readouterr().out


def test_shipped_templates_parse():
    for f in ("config.toml.example", os.path.join("tests", "config_custom.toml")):
        g = options.Get()
        g.load_toml(os.path.join(ROOT, f))
        assert isinstance(g.port, str)
    g = options.Get()
    g.load_toml(os.path.join(ROOT, "config.toml.example"))
    assert g.version.startswith("mainnet") and g.rest_api is True
