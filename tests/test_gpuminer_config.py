"""GPU-miner TOML configuration (gpuminer/minerconfig.py).

Proves the shipped miner.toml / opencl_alt/config.toml load to the settings the old key=value files gave,
the config_custom.toml key-by-key overlay, and the one-time verified migration of a legacy miner.txt /
config.txt (old file kept as .migrated; a lossy conversion writes nothing). Hermetic: pytest tmp dirs; no
GPU, pyopencl or CUDA needed. Run: python3 -m pytest tests/test_gpuminer_config.py -v
"""
import importlib.util
import os
import re
import shutil

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GPU = os.path.join(ROOT, "gpuminer")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


minerconfig = _load("minerconfig", os.path.join(GPU, "minerconfig.py"))
ocl_options = _load("ocl_options", os.path.join(GPU, "opencl_alt", "options.py"))
OCL_SCHEMA = {k: t for k, (_, t) in ocl_options.SCHEMA.items()}


def _optihash_schema():
    src = open(os.path.join(GPU, "optihash.py")).read()
    return eval(re.search(r"^SCHEMA = (\{.*?\})$", src, re.S | re.M).group(1))


OPTIHASH_SCHEMA = _optihash_schema()

# the pre-TOML files these replaced, verbatim
LEGACY_MINER_TXT = (
    "port=8525\nmining_ip=127.0.0.1\nmining_threads=1\ntor=0\n"
    "miner_address=0a83f9b7aa6dabf986ab2f81c857d036c47e367940dc49093873614d\n"
    "max_diff=150\nminer_name=cuda_gpu\nnonce_time=10\nhashcount=20000"
)
LEGACY_OCL_TXT = (
    "port=5658\ngenesis=4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed\nverify=0\n"
    "version=mainnet0016\nversion_allow=mainnet0016,mainnet0017\nthread_limit=75\npool_ip=127.0.0.1\n"
    "rebuild_db=0\ndebug=0\ndebug_level=WARNING\npurge=1\npause=6\nmining_threads=6\ndiff_recalc=50000\n"
    "ban_threshold=30\ntor=0\nminer_sync=1\nallowed=127.0.0.1,192.168.0.1,any\nmining_pool=0\n"
    "pool_address=4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed\npool_percentage=75\n"
    "node_ip=127.0.0.1\nlight_ip=127.0.0.1\nreveal_address=yes\naccept_peers=yes\nbanlist=127.1.2.3\n"
    "whitelist=127.1.2.4\nnodes_ban_reset=5\nopencl_hash_count=500\nopencl_timeout=300\n"
    "opencl_thread_multiplier=2\nopencl_full_check=1\nopencl_disable_device=12"
)


def test_shipped_miner_toml_matches_the_old_miner_txt(tmp_path):
    (tmp_path / "miner.txt").write_text(LEGACY_MINER_TXT)
    old = minerconfig.coerce(minerconfig.parse_legacy(str(tmp_path / "miner.txt")), OPTIHASH_SCHEMA)
    assert minerconfig.load_toml(os.path.join(GPU, "miner.toml"), OPTIHASH_SCHEMA) == old
    assert old["port"] == "8525" and old["nonce_time"] == 10


def test_shipped_opencl_config_matches_the_old_config_txt(tmp_path):
    (tmp_path / "config.txt").write_text(LEGACY_OCL_TXT)
    old = minerconfig.coerce(minerconfig.parse_legacy(str(tmp_path / "config.txt")), OCL_SCHEMA)
    assert minerconfig.load_toml(os.path.join(GPU, "opencl_alt", "config.toml"), OCL_SCHEMA) == old
    assert len(old) == len(OCL_SCHEMA)                               # every key the loader knows is shipped


def test_opencl_get_sets_the_attributes_the_miner_reads(tmp_path):
    shutil.copy(os.path.join(GPU, "opencl_alt", "config.toml"), tmp_path)
    g = ocl_options.Get()
    g.read(str(tmp_path))
    assert g.port == "5658" and g.verify_conf == 0 and g.sync_conf == 1 and g.pool_conf == 0
    assert g.version_allow == ["mainnet0016", "mainnet0017"] and g.banlist == ["127.1.2.3"]
    assert g.opencl_disable_device_conf == [12] and g.opencl_hash_count_conf == 500
    assert g.pool_address_conf == "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed"


def test_opencl_custom_overlays_key_by_key(tmp_path):
    shutil.copy(os.path.join(GPU, "opencl_alt", "config.toml"), tmp_path)
    (tmp_path / "config_custom.toml").write_text("mining_pool = 1\nopencl_disable_device = []\n")
    g = ocl_options.Get()
    g.read(str(tmp_path))
    assert g.pool_conf == 1 and g.opencl_disable_device_conf == []
    assert g.port == "5658"                                          # untouched keys come from config.toml


def test_coercion_accepts_either_spelling(tmp_path):
    (tmp_path / "m.toml").write_text('port = 8525\ntor = true\nnonce_time = "10"\nbogus = 1\n')
    assert minerconfig.load_toml(str(tmp_path / "m.toml"), OPTIHASH_SCHEMA) == \
        {"port": "8525", "tor": 1, "nonce_time": 10}


def test_legacy_miner_txt_is_migrated_once(tmp_path):
    (tmp_path / "miner.txt").write_text("# my rig\n" + LEGACY_MINER_TXT + "\n")
    values = minerconfig.load(str(tmp_path), "miner.toml", OPTIHASH_SCHEMA, legacy="miner.txt")
    assert values["miner_address"] == "0a83f9b7aa6dabf986ab2f81c857d036c47e367940dc49093873614d"
    assert not (tmp_path / "miner.txt").exists() and (tmp_path / "miner.txt.migrated").exists()
    assert minerconfig.load_toml(str(tmp_path / "miner.toml"), OPTIHASH_SCHEMA) == values
    (tmp_path / "miner.txt").write_text("port=1\n")                  # a stray txt beside the toml is ignored
    assert minerconfig.load(str(tmp_path), "miner.toml", OPTIHASH_SCHEMA, legacy="miner.txt")["port"] == "8525"


def test_legacy_opencl_config_and_custom_are_migrated(tmp_path):
    (tmp_path / "config.txt").write_text(LEGACY_OCL_TXT)
    (tmp_path / "config_custom.txt").write_text("mining_pool=1\n")
    g = ocl_options.Get()
    g.read(str(tmp_path))
    assert g.pool_conf == 1 and g.opencl_disable_device_conf == [12]
    for name in ("config", "config_custom"):
        assert (tmp_path / (name + ".toml")).exists() and (tmp_path / (name + ".txt.migrated")).exists()


def test_failed_migration_writes_nothing(tmp_path, monkeypatch):
    (tmp_path / "miner.txt").write_text(LEGACY_MINER_TXT)
    monkeypatch.setattr(minerconfig, "build_toml", lambda values, header="": 'port = "9"\n')
    with pytest.raises(ValueError):
        minerconfig.migrate_legacy(str(tmp_path / "miner.txt"), str(tmp_path / "miner.toml"), OPTIHASH_SCHEMA)
    assert (tmp_path / "miner.txt").exists()
    assert not (tmp_path / "miner.toml").exists() and not (tmp_path / "miner.toml.tmp").exists()


def test_legacy_values_are_taken_verbatim(tmp_path):
    # the old loader used str.lstrip(prefix), which strips a CHARACTER SET: a value starting with one of the
    # key's letters lost it. The migration takes everything after the first '='.
    (tmp_path / "c.txt").write_text("pool_address=add4\nversion=v1\n")
    assert minerconfig.coerce(minerconfig.parse_legacy(str(tmp_path / "c.txt")), OCL_SCHEMA) == \
        {"pool_address": "add4", "version": "v1"}
