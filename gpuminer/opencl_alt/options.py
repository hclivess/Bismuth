"""Configuration loader for the alternative OpenCL GPU miner.

Defines ``Get``, a standalone config reader for the ``opencl_alt`` miner. It reads ``config.toml`` (next
to this file), overlaid key by key by ``config_custom.toml`` when present, and exposes each setting as an
attribute -- node/pool connection details, mining-thread and pool-payout options, and the OpenCL tuning
knobs (hash count, timeout, thread multiplier, per-device disable list, etc.). A pre-TOML ``config.txt``
/ ``config_custom.txt`` is migrated once (``gpuminer/minerconfig.py``). A miner-local copy independent
of the node's own ``options`` module.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # gpuminer/, for minerconfig
import minerconfig  # noqa: E402

# config key -> (attribute, type). The attribute names are the ones miner.py / clminer.py read.
SCHEMA = {
    "port": ("port", "str"),
    "genesis": ("genesis_conf", "str"),
    "verify": ("verify_conf", "int"),
    "version": ("version_conf", "str"),
    "version_allow": ("version_allow", "list"),
    "thread_limit": ("thread_limit_conf", "int"),
    "rebuild_db": ("rebuild_db_conf", "int"),
    "debug": ("debug_conf", "int"),
    "purge": ("purge_conf", "int"),
    "pause": ("pause_conf", "str"),
    "ban_threshold": ("ban_threshold", "int"),
    "tor": ("tor_conf", "int"),
    "debug_level": ("debug_level_conf", "str"),
    "allowed": ("allowed_conf", "str"),
    "pool_ip": ("pool_ip_conf", "str"),
    "miner_sync": ("sync_conf", "int"),
    "mining_threads": ("mining_threads_conf", "str"),
    "diff_recalc": ("diff_recalc_conf", "int"),
    "mining_pool": ("pool_conf", "int"),
    "pool_address": ("pool_address_conf", "str"),
    "opencl_hash_count": ("opencl_hash_count_conf", "int"),
    "opencl_timeout": ("opencl_timeout_conf", "int"),
    "opencl_thread_multiplier": ("opencl_thread_multiplier_conf", "int"),
    "opencl_full_check": ("opencl_full_check_conf", "int"),
    "opencl_disable_device": ("opencl_disable_device_conf", "intlist"),
    "pool_percentage": ("pool_percentage_conf", "int"),
    "node_ip": ("node_ip_conf", "str"),
    "light_ip": ("light_ip_conf", "str"),
    "reveal_address": ("reveal_address", "str"),
    "accept_peers": ("accept_peers", "str"),
    "banlist": ("banlist", "list"),
    "nodes_ban_reset": ("nodes_ban_reset", "int"),
    "whitelist": ("whitelist", "str"),
}


class Get:
    def read(self, directory=HERE):
        """Load the miner config and set one attribute per recognised key."""
        values = minerconfig.load(directory, "config.toml", {k: t for k, (_, t) in SCHEMA.items()},
                                  legacy="config.txt", custom="config_custom.toml",
                                  legacy_custom="config_custom.txt")
        for key, value in values.items():
            setattr(self, SCHEMA[key][0], value)
