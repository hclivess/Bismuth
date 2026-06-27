"""Node configuration loader.

Defines ``Get``, the object that reads the node's configuration from
``config.txt`` (with ``config_custom.txt`` overrides) and exposes each setting
as a typed attribute -- ports, ledger/hyperblock paths, RAM/full-ledger mode,
debug level, peer ban thresholds, Tor toggle, mempool options, the opt-in REST
API switch, and so on. A typed schema maps each option name to its expected
type so values are coerced consistently. Nearly every other module obtains its
runtime settings through this loader.
"""

import json
import os
import os.path as path
from sys import exit

try:
    import tomllib  # stdlib on Python 3.11+ (the node targets 3.12); read-only TOML parser
except ImportError:  # pragma: no cover - older interpreters degrade gracefully to legacy config.txt
    tomllib = None


def _coerce_tor_mode(value):
    """Tri-state Tor mode, backward-compatible with the old boolean tor flag (doc/38):
    False/0/no/off/'' -> 'off' (clearnet, the default); True/1/yes/on/external -> 'external'
    (route through an existing daemon, the old tor=True behavior); 'managed' -> 'managed'
    (the node launches+owns its own tor); anything unrecognized -> 'off' (safe: never silently routes)."""
    s = str(value).strip().lower()
    if s == "managed":
        return "managed"
    if s in ("external", "true", "1", "yes", "on"):
        return "external"
    return "off"


class Get:
    # "param_name":["type"] or "param_name"=["type","property_name"]
    vars = {
        "port": ["str"],
        "verify": ["bool", "verify"],
        "testnet": ["bool"],
        "regnet": ["bool"],
        "heavy": ["bool"],
        "version": ["str", "version"],
        "version_allow": ["list"],
        "thread_limit": ["int", "thread_limit"],
        "rebuild_db": ["bool", "rebuild_db"],
        "debug": ["bool", "debug"],
        "purge": ["bool", "purge"],
        "pause": ["int", "pause"],
        "ledger_path": ["str", "ledger_path"],
        "hyper_path": ["str", "hyper_path"],
        "hyper_recompress": ["bool", "hyper_recompress"],
        "full_ledger": ["bool", "full_ledger"],
        "ban_threshold": ["int"],
        "tor": ["str", "tor"],            # tri-state: off | external | managed (doc/38); coerced from the old bool
        "tor_socks_host": ["str"],
        "tor_socks_port": ["int"],
        "tor_control_port": ["int"],
        "tor_binary": ["str"],
        "tor_onion": ["bool"],
        "tor_onion_key": ["str"],
        "tor_required": ["bool"],
        "tor_dial_timeout": ["int"],
        "tor_data_dir": ["str"],
        "debug_level": ["str", "debug_level"],
        "allowed": ["str", "allowed"],
        "ram": ["bool", "ram"],
        "node_ip": ["str", "node_ip"],
        "light_ip": ["dict"],
        "reveal_address": ["bool"],
        "accept_peers": ["bool"],
        "banlist": ["list"],
        "whitelist": ["list"],
        "nodes_ban_reset": ["int"],
        "mempool_allowed": ["list"],
        "terminal_output": ["bool"],
        "log_color": ["bool"],
        "gui_scaling": ["str"],
        "mempool_ram": ["bool"],
        "egress": ["bool"],
        "trace_db_calls": ["bool"],
        "heavy3_path": ["str"],
        "mempool_path": ["str"],
        "old_sqlite": ["bool"],
        "mandatory_message": ["list"],
        "rollback_depth": ["int"],
        "rest_api": ["bool"],
        "rest_api_port": ["int"],
        "rest_api_write": ["bool"],
        "rest_api_proxy": ["bool"],
        "rest_api_geo": ["bool"],
        "rest_api_market": ["bool"],
        "api_sync": ["bool"],
        "api_sync_source": ["str"],
        "autoheal": ["bool"],
        "autoheal_interval": ["int"],
        "diff_divergence_detect": ["bool"],
        "diff_divergence_pause_mining": ["bool"],
        "diff_divergence_autoheal": ["bool"],
        "diff_divergence_interval": ["int"],
        "assume_valid_height": ["int"],
        "validation_exceptions_file": ["str"],
        "sync_from_genesis": ["bool"],
        "rollback_consensus": ["bool"],
        "rollback_consensus_threshold": ["int"],
        "rollback_consensus_min_peers": ["int"],
        "rollback_consensus_min_reputable": ["int"],
        "ledger_integer_amounts": ["bool"],
        "bootstrap_url": ["str"],
        "bootstrap_file": ["str"],
        "snapshot_serve": ["bool"],
        "bootstrap_p2p": ["bool"],
        "snapshot_path": ["str"],
        "bootstrap_p2p_peers": ["list"],
        "block_store": ["bool"],
        "fork_signal": ["bool"],
        "fork_window": ["int"],
        "fork_boundary": ["int"],
        "fork_bury": ["int"],
        "mine": ["bool"],
        "rpc_bitcoin": ["bool"],
        "rpc_bitcoin_port": ["int"],
        "rpc_ethereum": ["bool"],
        "rpc_ethereum_port": ["int"],
        "balance_index": ["bool"],
        "balance_index_consensus": ["str"],
        "txid_index_consensus": ["str"],
        "pk_registry_consensus": ["str"],
        "parity_strict": ["bool"],
        "vm": ["bool"],
        "shield": ["bool"],
        "token_index": ["bool"],
    }

    # Optional default values so we don't bug if they are not in the config.
    # For compatibility
    defaults = {
        "testnet": False,
        "regnet": False,
        "heavy": True,
        "trace_db_calls": False,
        "mempool_ram": True,
        "log_color": True,  # ANSI color on the stdout/journald log by level (see log.ColoredFormatter); honors NO_COLOR env. File logs stay plain.
        "heavy3_path": "./heavy3a.bin",
        "mempool_path": "./mempool.db",
        "old_sqlite": False,
        "rollback_depth": 30,  # max blocks the node will roll back to rejoin a longer chain (see doc/14)
        "rest_api": False,     # opt-in modern parallel REST API (see doc/15); off by default
        "rest_api_port": 5659,
        "tor": "off",          # Tor mode (doc/38): off (clearnet) | external | managed; default clearnet
        "tor_socks_host": "127.0.0.1",
        "tor_socks_port": 9050,   # external-mode SOCKS port (the old hardcoded default); managed derives its own
        "tor_control_port": 0,    # 0 = auto (managed)
        "tor_binary": "",         # empty = discover 'tor' on PATH
        "tor_onion": False,       # managed: publish an ephemeral v3 hidden service for inbound
        "tor_onion_key": "static/tor_onion_key",  # persist the onion key for a stable .onion (0600)
        "tor_required": False,    # False = graceful clearnet fallback on tor failure; True = fail-fast
        "tor_dial_timeout": 30,   # bounded connect under tor (replaces the old unbounded None)
        "tor_data_dir": "",       # empty = a node-owned dir for managed tor's DataDirectory
        "rest_api_write": False,  # POST /api/transaction (tx submission over REST, the post-hardfork path); off by default
        "rest_api_proxy": True,   # GET /api/proxy same-origin relay so the https explorer can browse http nodes (read-only, SSRF-guarded); on by default
        "rest_api_geo": True,     # GET /api/stats/geo peer geolocation for the node map (ip-api.com, server-side cached); on by default
        "rest_api_market": True,  # GET /api/stats/market price/market-cap (coingecko, server-side TTL-cached); on by default
        "api_sync": False,        # opt-in: catch up the chain over a peer's REST API instead of the socket (doc/17, api_sync_worker); off by default
        "api_sync_source": "",    # the REST peer to fast-sync from, "host:rest_port" (used when api_sync=True)
        "autoheal": True,         # live self-heal of tip sequence/dupe corruption WITHOUT a restart (chain_ops.autoheal_live); ON by default
        "autoheal_interval": 300, # seconds between live autoheal checks (cheap recent-tail scan; repairs only on a real break)
        # doc/35 peer-difficulty divergence detector + guarded self-heal (#23). The detector AND the guarded
        # AUTOHEAL are ENABLED (operator decision); pause_mining stays opt-in. On a CONFIRMED peer divergence
        # (>=3-peer quorum, 75% supermajority, 3-cycle debounce) the node writes the one-shot rollback_to
        # trigger + restarts to roll back below the corruption and resync, so difficulty re-derives from a
        # clean base. Restart-loops are impossible: a per-ledger PERMANENT lifetime cap (2 heals) -> advisory-
        # only forever, plus a 1h cooldown + bounded depth + rollback_allowed anti-sybil gate. The detector
        # reads cached node.difficulty + polls peers over HTTP — it NEVER scans the ledger.
        "diff_divergence_detect": True,         # run the detector + loud-log + plugin alert on confirmed divergence
        "diff_divergence_pause_mining": False,  # OPT-IN: set node.mining_paused on confirmed divergence (auto-clears on CLEAN)
        "diff_divergence_autoheal": True,       # ENABLED: guarded rollback_to + restart heal (cooldown/lifetime-cap/bounded-depth)
        "diff_divergence_interval": 300,        # seconds between divergence poll cycles
        "assume_valid_height": 4000000,     # doc/30 from-genesis sync: trust horizon. At/below it skip per-item validation (sig/timestamp/pow/dup/overspend), ANCHORED by the hardcoded block-hash checkpoints (mismatch halts). Inert for a synced node (never re-digests historical heights) and for networks without checkpoints (regnet/testnet). 0 = off = full validation everywhere.
        "validation_exceptions_file": "",   # doc/30: optional JSON of historical validation waivers (coin rescues / fork-edge edits), merged over the in-source MAINNET_EXCEPTIONS; empty = built-in registry only
        "sync_from_genesis": False,         # doc/30: on a fresh ledger, seed the canonical genesis block and sync the chain from block 1 instead of downloading a snapshot (trusted prefix below assume_valid_height anchored by checkpoints). No effect once the ledger has blocks.

        "rollback_consensus": True,          # AUTO-RECOVERY: reputation-gated deep rollback so a forked node rejoins on its own (doc/14); ON by default (replaces the rigid rollback_depth stranding)
        "rollback_consensus_threshold": 75,  # % peer agreement required to allow a deep rollback
        "rollback_consensus_min_peers": 3,   # min peers in consensus to allow a deep rollback
        "rollback_consensus_min_reputable": 1,  # min PROVEN (positive-reputation) peers required for a deep auto-recovery rollback (anti-sybil)
        "ledger_integer_amounts": False,     # store amount/fee/reward as integer atomic units (doc/16); off by default
        # Bootstrap source. The historical hardcoded host can disappear (it did), so the URL is
        # configurable and a locally-provided archive takes precedence (see chain_ops.bootstrap).
        "bootstrap_url": "https://bismuth.cz/ledger.tar.gz",
        "bootstrap_file": "",                # path to a local bootstrap archive; if set/present, used instead of downloading
        "snapshot_serve": False,             # doc/43: serve a pre-built snapshot tarball over /api/snapshot[/info]
        "bootstrap_p2p": False,              # doc/43: fetch the bootstrap snapshot from peers (sha256-verified) before the central URL
        "snapshot_path": "static/ledger-snapshot.tar.gz",  # the pre-built snapshot this node serves (build via scripts/snapshot.py)
        "bootstrap_p2p_peers": [],           # optional explicit ["host:rest_port", ...] snapshot sources
        "block_store": False,                # opt-in: also mirror block bodies into an LMDB store (doc/17 phase 7); off by default
        "fork_signal": False,                # hf2: this node's miner stamps the readiness signal into the coinbase nonce
        "fork_window": 1000,                 # consecutive all-signalled blocks to lock in (regnet overrides small)
        "fork_boundary": 1000,               # activate on the next multiple of this
        "fork_bury": 30,                     # blocks between lock-in and activation (reorg safety)
        "mine": False,                       # opt-in built-in solo miner (miner.py); off by default
        "rpc_bitcoin": False,                # opt-in bitcoind-compatible JSON-RPC adapter (doc/17); off by default
        "rpc_bitcoin_port": 8332,
        "rpc_ethereum": False,               # opt-in eth_* compatibility shim (doc/17); off by default
        "rpc_ethereum_port": 8545,
        "balance_index": False,              # opt-in maintained O(1) balance index for the DISPLAY read path (doc/17); off by default. Consensus stays on ledger_balance3.
        "balance_index_consensus": "off",    # doc/26 stage 4: off|shadow|primary. off=ledger_balance3 (SQLite) authoritative (default/mainnet). shadow=compute the LMDB balance_index too + compare at the overspend read (warn, or raise under parity_strict). primary=index authoritative, still cross-check SQLite + RAISE on mismatch (halt > inflate). Needs balance_index + integer-units + post-fork; never engages pre-fork.
        "parity_strict": False,              # doc/26 stage 4: turn store/index parity mismatches (balance seam) into a raised AssertionError instead of a log warning — for CI/regnet, NEVER prod.
        "vm": False,                         # opt-in decentralized-apps VM (doc/17); executes vm: txs POST-FORK only. Off by default; inert until hf2 activates.
        "shield": False,                     # opt-in shielded value (doc/22); validates shield: txs POST-FORK only. Off by default; inert until hf2 activates.
        "token_index": False,                # opt-in LMDB token/alias side-index (doc/26 stage 2), replacing the SQLite index.db projection. Off by default (mainnet stays on index.db pre-fork); becomes the post-fork home for tokens/aliases.
        "mandatory_message": {
            "Address": "Comment - Dict for addresses that require a message. tx to these addresses withjout a message will not be accepted by mempool.",
            "f6c0363ca1c5aa28cc584252e65a63998493ff0a5ec1bb16beda9bac": "qTrade Exchange needs a message to route the deposit to your account",
            "d11ea307ea6de821bc28c645b1ff8dd25c6e8a9f70b3a6aeb9928754": "VGate/ViteX Exchange needs a message to route the deposit to your account",
            "14c1b5851634f0fa8145ceea1a52cabe2443dc10350e3febf651bd3a": "Graviex Exchange needs a message to route the deposit to your account",
            "1a174d7fdc2036e6005d93cc985424021085cc4335061307985459ce": "Finexbox Exchange needs a message to route the deposit to your account",
            "49ca873779b36c4a503562ebf5697fca331685d79fd3deef64a46888": "Tradesatoshi is no more listing bis but needed a message to route the deposit to your account",
            "edf2d63cdf0b6275ead22c9e6d66aa8ea31dc0ccb367fad2e7c08a25": "Old Cryptopia address, memo",
        },  # setup here by safety, but will use the json if present for easier updates.
    }

    def _set_from_raw(self, left, right):
        """Coerce a raw STRING value (the legacy key=value format) per the ``vars`` schema and set it.
        Unknown keys are silently skipped; the renamed-property edge (params[1]) is honored. Extracted
        verbatim from the old load_file body so the text path is byte-identical."""
        if "mempool_ram_conf" == left:
            print(
                "Inconsistent config, param is now mempool_ram in config.txt"
            )
            exit()
        if not left in self.vars:
            # Warn for unknown param?
            return
        if left == "tor":
            # tri-state Tor mode, backward-compatible with the legacy boolean (doc/38)
            setattr(self, "tor", _coerce_tor_mode(right))
            return
        params = self.vars[left]
        if params[0] == "int":
            right = int(right)
        elif params[0] == "dict":
            try:
                right = json.loads(right)
            except:  # compatibility
                right = [item.strip() for item in right.split(",")]
        elif params[0] == "list":
            right = [item.strip() for item in right.split(",")]
        elif params[0] == "bool":
            if right.lower() in ["false", "0", "", "no"]:
                right = False
            else:
                right = True

        else:
            # treat as "str"
            pass
        if len(params) > 1:
            # deal with properties that do not match the config name.
            left = params[1]
        setattr(self, left, right)

    def _finalize(self):
        """Shared loader tail: hardcode genesis + backfill any schema key the file did not set from
        ``defaults``. Runs after EVERY load_file / load_toml, so both formats yield an identical Config."""
        # Default genesis to keep compatibility
        self.genesis = "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed"
        for key, default in self.defaults.items():
            if key not in self.__dict__:
                setattr(self, key, default)
        # print(self.__dict__)

    def load_file(self, filename):
        """Legacy key=value loader (config.txt / config_custom.txt). Behaviour-identical to before; the
        per-key coercion now lives in _set_from_raw and the genesis/defaults tail in _finalize."""
        # print("Loading",filename)
        with open(filename) as fp:
            for line in fp:
                if "=" in line:
                    left, right = map(str.strip, line.rstrip("\n").split("="))
                    self._set_from_raw(left, right)
        self._finalize()

    def load_toml(self, filename):
        """Modern TOML loader (config.toml / config_custom.toml). TOML yields NATIVE types, so values are
        taken AS-IS when they already match the schema type — no string re-coercion (and so none of the
        legacy false-set / Norway-style footguns). The only cross-type edge is ``port`` (schema type str)
        which an operator may write as an int. Unknown keys are skipped exactly like the legacy loader, and
        load terminates in the SAME _finalize — so a config.toml and the equivalent config.txt produce a
        byte-identical Config object (asserted in tests/test_config_toml.py)."""
        if tomllib is None:  # pragma: no cover
            raise RuntimeError("TOML config requires Python 3.11+ (tomllib); use config.txt instead")
        with open(filename, "rb") as fp:
            data = tomllib.load(fp)
        for left, value in data.items():
            if not left in self.vars:
                continue
            if left == "tor":
                # tri-state Tor mode (accepts a TOML bool or a string), doc/38
                setattr(self, "tor", _coerce_tor_mode(value))
                continue
            params = self.vars[left]
            if params[0] == "str" and not isinstance(value, str):
                value = str(value)   # e.g. TOML `port = 5658` (int) -> "5658" (schema type is str)
            if len(params) > 1:
                # deal with properties that do not match the config name.
                left = params[1]
            setattr(self, left, value)
        self._finalize()

    def read(self):
        # config.txt is gitignored (it holds the node's live/local settings, incl. rest_api). If it has
        # gone missing -- e.g. a git checkout / worktree switch removed it -- auto-restore it from the
        # tracked config.txt.example template, so a missing config can't crash-loop the node at startup
        # (the FileNotFoundError edge exposed during the v4.5.0.x DB-resilience work).
        if not path.exists("config.txt") and path.exists("config.txt.example"):
            import shutil
            shutil.copyfile("config.txt.example", "config.txt")
            print("config.txt was missing -- restored it from config.txt.example")
        # Base layer: prefer the modern config.toml when the operator has opted in by creating one (doc/11);
        # otherwise the legacy config.txt path is byte-identical to before, so an existing node is untouched.
        if tomllib is not None and path.exists("config.toml"):
            self.load_toml("config.toml")
        else:
            self.load_file("config.txt")
        # then override with optional custom config (the regnet test harness drops in config_custom.txt).
        # BISMUTH_IGNORE_CONFIG_CUSTOM=1 lets the systemd mainnet service ignore a leftover test override
        # so it always boots mainnet regardless of a stray config_custom.* (see scripts/install-node-service.sh).
        # Same 3-layer precedence as before; config_custom.toml is preferred over .txt when present.
        if not os.environ.get("BISMUTH_IGNORE_CONFIG_CUSTOM"):
            if tomllib is not None and path.exists("config_custom.toml"):
                self.load_toml("config_custom.toml")
            elif path.exists("config_custom.txt"):
                self.load_file("config_custom.txt")
        file_name = "./mandatory_message.json"
        if path.isfile(file_name):
            try:
                with open(file_name) as fp:
                    data = json.load(fp)
                    if type(data) != dict:
                        raise RuntimeWarning("Bad file format")
                    self.mandatory_message = data
                    print("mandatory_message file loaded")
            except Exception as e:
                print("Error loading mandatory_message.json {}".format(e))

        """
        if "regnet" in self.version:
            print("Regnet, forcing ram = False")
            self.ram = False
        """
