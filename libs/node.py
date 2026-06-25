"""Central mutable state object for the Bismuth node.

A single :class:`Node` instance is created at process start (``node.py``:
``node = node.Node()``) and threaded through essentially every subsystem -
connection handlers, the digest/validation path, mempool, miner, peers,
the REST/RPC servers and the various DB handlers. Rather than passing dozens of
parameters around, those modules read and mutate fields on this shared object
(e.g. ``node.last_block``, ``node.peers``, ``node.db_lock``, ``node.IS_STOPPING``).

The attributes are deliberately initialised to ``None``/placeholder values here
and filled in later:

    * configuration fields (``port``, ``ledger_path``, ``ram`` ...) are copied
      from the parsed ``options.Get()`` config in ``node.py`` after construction;
    * runtime fields (``last_block``, ``difficulty``, ``last_block_timestamp`` ...)
      are updated as the chain advances;
    * the holder objects ``logger``, ``keys`` and ``peers`` are assigned their
      ``libs.logger.Logger`` / ``libs.keys.Keys`` / peers-handler instances at
      startup.

IMPORTANT: many further attributes are attached to the instance dynamically by
other modules (``node.log_color``, ``node.fork_height``, ``node.block_store``,
``node.vm_state`` ...). For that reason this class must stay open - do NOT add
``__slots__`` or otherwise restrict attribute assignment.
"""

import threading
import queue
import sys
import platform


class Node:
    """Shared, mutable node state. See module docstring for the overall role.

    The attribute groups (in ``__init__`` order) are:

    Core services / handles:
        logger: ``libs.logger.Logger`` holder (its ``app_log`` is the logger).
        db_lock (threading.Lock): serialises writes across ledger.db/hyper.db.
        plugin_manager: the loaded plugin manager, if any.
        peers: the peers handler (peer list, consensus, reputation).
        apihandler: the legacy API handler instance.
        keys: ``libs.keys.Keys`` holder for the wallet key material.
        q (queue.Queue): generic inter-thread work queue.

    Versioning / lifecycle:
        app_version, version, version_allow: node + protocol version strings.
        startup_time: process start timestamp.
        IS_STOPPING (bool): set True to request a graceful shutdown.
        syncing (list): tracks in-progress sync peers.
        checkpoint (int): last checkpoint height.

    Network mode (exactly one is True at runtime):
        is_testnet, is_regnet, is_mainnet (bool).

    Chain head (HDD vs RAM differ in ram mode):
        hdd_block, hdd_hash: head as persisted on disk.
        last_block, last_block_hash: head as known in memory.
        last_block_timestamp, last_block_ago: timing of the head block.
        difficulty: current difficulty tuple/value.

    Filesystem / DB paths:
        hyper_path, ledger_path, ledger_ram_file, index_db,
        peerfile, peerfile_suggested, ledger_temp, hyper_temp.
        hyper_recompress, recompress: hyperblock recompression flags.

    Config-derived runtime options (copied from ``options.Get()``):
        debug_level, port, verify, thread_limit, rebuild_db, debug, pause,
        tor, ram, reveal_address, terminal_output, log_color, egress, genesis,
        accept_peers.

    Environment:
        py_version (int): packed major/minor/micro of the running interpreter.
        linux (bool | None): True on Linux (see :meth:`platform`), else None.
    """

    def platform(self) -> bool:
        # Returns True on Linux; otherwise falls off the end and returns None
        # (the implicit return is intentional and relied upon by ``self.linux``).
        if "Linux" in platform.system():
            return True

    def __init__(self) -> None:
        self.logger = None
        self.db_lock = threading.Lock()
        self.app_version = None
        self.startup_time = None
        self.version_allow = None
        self.hdd_block = None  # in ram mode, this differs from node.last_block
        self.hdd_hash = None  # in ram mode, this differs from node.last_block_hash
        self.last_block_hash = None  # in ram mode, this differs from node.hdd_hash
        self.last_block = None  # in ram mode, this differs from node.hdd_block
        self.plugin_manager = None
        self.peers = None
        self.IS_STOPPING = False
        self.apihandler = None
        self.syncing = []
        self.checkpoint = 0

        self.is_testnet = False
        self.is_regnet = False
        self.is_mainnet = False

        self.hyper_recompress = True
        self.hyper_path = None
        self.ledger_path = None
        self.ledger_ram_file = None
        self.peerfile = None
        self.peerfile_suggested = None
        self.index_db = None
        self.version = None
        self.version_allow = None

        self.version = None
        self.debug_level = None
        self.port = None
        self.verify = None
        self.thread_limit = None
        self.rebuild_db = None
        self.debug = None
        self.pause = None
        self.ledger_path = None
        self.hyper_path = None
        self.tor = None              # back-compat truthy bool: any active tor mode (doc/38)
        self.tor_mode = "off"        # off | external | managed
        self.tor_manager = None      # tor_manager.TorManager when a tor mode is active, else None
        self.tor_socks_host = "127.0.0.1"
        self.tor_socks_port = 9050
        self.tor_control_port = 0
        self.tor_binary = ""
        self.tor_onion = False
        self.tor_onion_key = "static/tor_onion_key"
        self.tor_required = False
        self.tor_dial_timeout = 30
        self.tor_data_dir = ""
        self.ram = None
        self.version_allow = None
        self.reveal_address = None
        self.terminal_output = None
        self.log_color = None
        self.egress = None
        self.genesis = None

        self.last_block_timestamp = None
        self.last_block_ago = None

        self.recompress = None

        self.accept_peers = True
        self.difficulty = None
        # doc/35: when the peer-difficulty divergence detector (opt-in pause) confirms our local difficulty
        # is wrong, the miner skips rounds (a wrong difficulty in either direction orphan-binds our blocks).
        # Default False (no pause); auto-cleared on the next CLEAN reading.
        self.mining_paused = False
        self._diffheal_armed = False  # once-per-boot guard: blocks a 2nd heal arm before the restart completes
        self.ledger_temp = None
        self.hyper_temp = None
        self.q = queue.Queue()
        self.py_version= int(str(sys.version_info.major) + str(sys.version_info.minor) + str(sys.version_info.micro))

        self.keys = None
        self.linux = self.platform()
