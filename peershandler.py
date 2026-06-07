"""
Peers handler module for Bismuth nodes
@EggPoolNet
Optimized version
"""

import random
import threading
from time import time
from collections import Counter

import regnet

# The Peers god-class is split into domain mixins (storage / pool / consensus / access) and
# recombined below; this module keeps the __slots__ + __init__ + the net-type helpers and the
# client_loop maintenance orchestrator.
from peers_storage import PeersStorageMixin
from peers_pool import PeersPoolMixin
from peers_consensus import PeersConsensusMixin
from peers_access import PeersAccessMixin

__version__ = "0.0.19"


class Peers(PeersStorageMixin, PeersPoolMixin, PeersConsensusMixin, PeersAccessMixin):
    """The peers manager. A thread safe peers manager"""

    __slots__ = ('app_log','config','logstats','node','peersync_lock','startup_time','reset_time','warning_list','stats',
                 'connection_pool','peer_opinion_dict','consensus_percentage','consensus',
                 'tried','peer_dict','peerfile','suggested_peerfile','banlist','whitelist','ban_threshold',
                 'ip_to_mainnet', 'peers', 'accept_peers', 'peerlist_updated', '_warning_counts',
                 '_connection_pool_set', '_c_class_cache', '_peer_dict_cache', '_cache_timestamp')

    def __init__(self, app_log, config=None, logstats=True, node=None):
        self.app_log = app_log
        self.config = config
        self.logstats = logstats
        self.peersync_lock = threading.Lock()
        self.startup_time = time()
        self.reset_time = self.startup_time
        self.warning_list = []
        self.stats = []
        self.peer_opinion_dict = {}
        self.consensus_percentage = 0
        self.consensus = None
        self.tried = {}
        self.peer_dict = {}
        self.ip_to_mainnet = {}
        self.connection_pool = []

        # Optimization: Add set for O(1) connection pool lookups
        self._connection_pool_set = set()
        # Optimization: Use Counter for warning counts
        self._warning_counts = Counter()
        # Optimization: Cache for C-class calculations
        self._c_class_cache = {}
        # Optimization: Cache for peer_dict operations
        self._peer_dict_cache = None
        self._cache_timestamp = 0

        # We store them apart from the initial config, could diverge somehow later on.
        self.banlist = config.banlist
        self.whitelist = config.whitelist
        self.ban_threshold = config.ban_threshold
        self.accept_peers = config.accept_peers

        self.peerfile = "peers.txt"
        self.suggested_peerfile = "suggested_peers.txt"
        self.peerlist_updated = False

        self.node = node

        if self.is_testnet:  # overwrite for testnet
            self.peerfile = "peers_test.txt"
            self.suggested_peerfile = "suggested_peers_test.txt"

        if self.is_regnet:  # regnet won't use any peer, won't connect. Kept for compatibility
            self.peerfile = regnet.REGNET_PEERS
            self.suggested_peerfile = regnet.REGNET_SUGGESTED_PEERS

    @property
    def is_testnet(self):
        """Helper to check if testnet or not. Only one place to change variable names and test"""
        if self.config.regnet:
            # regnet takes over testnet
            return False
        if self.config.testnet:
            return True
        return "testnet" in self.config.version

    @property
    def is_regnet(self):
        """Helper to check if regnet or not. Only one place to change variable names and test"""
        if self.config.regnet:
            # regnet takes over testnet
            return True
        return "regnet" in self.config.version

    def dict_shuffle(self, dictinary):
        l = list(dictinary.items())
        random.shuffle(l)
        return dict(l)

    def status_dict(self):
        """Returns a status as a dict"""
        status = {"version": self.config.VERSION, "stats": self.stats}
        return status

    def client_loop(self, node, this_target):
        """Manager loop called every 30 sec. Handles maintenance"""
        try:
            # Optimization: Cache peer_dict for iteration
            current_peers = dict(self.dict_shuffle(self.peer_dict))

            for host, value in current_peers.items():
                port = int(value)

                if self.is_testnet:
                    port = 2829

                if threading.active_count() / 3 < self.config.thread_limit and self.can_connect_to(host, port):
                    self.app_log.info(f"Will attempt to connect to {host}:{port}")
                    self.add_try(host, port)
                    t = threading.Thread(target=this_target, args=(host, port, node),
                                        name=f"out_{host}_{port}")
                    self.app_log.info(f"---Starting a client thread {threading.currentThread()} ---")
                    t.daemon = True
                    t.start()

            # Optimization: Use cached values for repeated checks
            pool_size = len(self._connection_pool_set)
            time_since_start = time() - self.startup_time

            if len(self.peer_dict) < 6 and time_since_start > 30:
                self.app_log.warning("Not enough peers in consensus, joining in peers suggested by other nodes")
                self.peer_dict.update(self.peers_get(self.suggested_peerfile))

            if pool_size < self.config.nodes_ban_reset and time_since_start > 15:
                self.app_log.warning(f"Only {pool_size} connections active, resetting banlist")
                self.banlist[:] = self.config.banlist
                self.warning_list.clear()
                self._warning_counts.clear()

            if pool_size < 10:
                self.app_log.warning(f"Only {pool_size} connections active, resetting the connection history")
                self.reset_tried()

            ban_size = len(self.banlist)
            if (self.config.nodes_ban_reset <= ban_size and
                pool_size <= ban_size and
                (time() - self.reset_time) > 600):
                self.app_log.warning(f"Less active connections ({pool_size}) than banlist ({ban_size}), "
                                   f"resetting banlist and tried list")
                self.banlist[:] = self.config.banlist
                self.warning_list.clear()
                self._warning_counts.clear()
                self.reset_tried()
                self.reset_time = time()

            self.app_log.warning("Status: Testing peers")
            self.peer_dict.update(self.peers_get(self.peerfile))

            # Testing peers
            self.peers_test(self.suggested_peerfile, self.peer_dict, strict=False)
            self.peers_test(self.peerfile, self.peer_dict, strict=True)

        except Exception as e:
            self.app_log.warning(f"Status: peers client loop skipped due to error: {e}")

    def status_log(self):
        """Prints the peers part of the node status"""
        if self.banlist:
            self.app_log.warning(f"Status: Banlist: {self.banlist}")
            self.app_log.warning(f"Status: Banlist Count : {len(self.banlist)}")
        if self.whitelist:
            self.app_log.warning(f"Status: Whitelist: {self.whitelist}")

        self.app_log.warning(f"Status: Known Peers: {len(self.peer_dict)}")
        self.app_log.info(f"Status: Tried: {self.tried}")
        self.app_log.info(f"Status: Tried Count: {len(self.tried)}")
        self.app_log.info(f"Status: List of Outbound connections: {self.connection_pool}")
        self.app_log.warning(f"Status: Number of Outbound connections: {len(self.connection_pool)}")
        if self.consensus:
            self.app_log.warning(f"Status: Consensus height: {self.consensus} = {self.consensus_percentage}%")
            self.app_log.warning(f"Status: Last block opinion: {self.peer_opinion_dict}")
            self.app_log.warning(f"Status: Total number of nodes: {len(self.peer_opinion_dict)}")