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
from peers_reputation import PeersReputationMixin

__version__ = "0.0.19"

# Fast recovery from isolation: when the number of OUTBOUND connections is at or below this floor, the
# node is effectively isolated (0-1 peers) or critically low and is at risk of falling behind consensus.
# In that one state BOTH halves of the recovery engage together (see Peers.is_isolated): client_loop
# bypasses the per-peer retry back-off and re-dials every known peer at once (reset_tried(aggressive=
# True)), AND the ConnectionManager shortens its loop to RECOVERY_LOOP_SECONDS so that redial-all pass
# repeats within seconds. Above this floor the gentle steady-state back-off and the normal cadence are
# both kept, so a well-connected node never hammers its peers. Coupling the cadence and the bypass on
# this single floor is the fix for the reverted attempt, where they were on two different counters.
OUTBOUND_RECOVERY_FLOOR = 4

# ConnectionManager loop cadence. Normally 30s between dial passes; while is_isolated() the manager
# polls every RECOVERY_LOOP_SECONDS so the redial-all pass repeats within seconds, not minutes. Defined
# here (next to the floor) so the floor and both cadences are one source of truth that connectionmanager
# imports -- the two modules can never disagree on the threshold or the timings.
NORMAL_LOOP_SECONDS = 30
RECOVERY_LOOP_SECONDS = 5


class Peers(PeersStorageMixin, PeersPoolMixin, PeersConsensusMixin, PeersAccessMixin, PeersReputationMixin):
    """The peers manager. A thread safe peers manager"""

    __slots__ = ('app_log','config','logstats','node','peersync_lock','startup_time','reset_time','warning_list','stats',
                 'connection_pool','peer_opinion_dict','consensus_percentage','consensus',
                 'tried','peer_dict','peerfile','suggested_peerfile','banlist','whitelist','ban_threshold',
                 'ip_to_mainnet', 'peers', 'accept_peers', 'peerlist_updated', '_warning_counts',
                 '_connection_pool_set', '_c_class_cache', '_peer_dict_cache', '_cache_timestamp',
                 '_reputation')

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
        self._reputation = {}            # peer_ip -> reputation score (peers_reputation)

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

    def outbound_count(self):
        """The AUTHORITATIVE number of live outbound connections: the length of the connection_pool
        LIST (the canonical record append_client/remove_client maintain). This is the single source of
        truth for "how isolated am I" -- deliberately NOT len(self._connection_pool_set), which is an
        O(1)-lookup optimization that can diverge from the list under churn and is what desynced the
        reverted recovery attempt."""
        return len(self.connection_pool)

    def is_isolated(self):
        """True when outbound connectivity has collapsed to the recovery floor or below.

        This ONE predicate is the shared trigger for the whole fast-recovery-from-isolation path: it is
        called by client_loop (to decide whether to wipe back-off and re-dial every known peer) AND by
        the ConnectionManager (to decide whether to use the 5s recovery cadence instead of 30s). Because
        both sites call this exact method over the exact same counter (outbound_count()), the bypass and
        the faster cadence can NEVER get out of sync -- the precise failure mode that regressed the
        reverted attempt (aggressive reset on _connection_pool_set, cadence on connection_pool)."""
        return self.outbound_count() <= OUTBOUND_RECOVERY_FLOOR

    def dict_shuffle(self, dictinary):
        l = list(dictinary.items())
        random.shuffle(l)
        return dict(l)

    def _order_by_recency(self, peers):
        """Order ``peers`` ({ip: port}) so recently-responsive peers come first, used only on the
        isolation-recovery path. A peer we have completed a version handshake with is in
        ``ip_to_mainnet`` (set by store_mainnet on a successful outbound/inbound connect); those are the
        peers most likely to still be up, so we dial them before the rest. Membership-only, no scoring
        and no I/O; consensus-neutral. Ties (and all unknown peers) keep their already-shuffled order, so
        we still spread load and never deterministically hammer one peer."""
        proven = {ip: port for ip, port in peers.items() if ip in self.ip_to_mainnet}
        rest = {ip: port for ip, port in peers.items() if ip not in self.ip_to_mainnet}
        proven.update(rest)
        return proven

    def status_dict(self):
        """Returns a status as a dict"""
        status = {"version": self.config.VERSION, "stats": self.stats}
        return status

    def client_loop(self, node, this_target):
        """Manager loop called every 30 sec (or every RECOVERY_LOOP_SECONDS while isolated). Maintenance."""
        try:
            # FAST RECOVERY FROM ISOLATION.
            # If outbound connections have collapsed to the recovery floor or below (just restarted, or
            # lost all peers), the per-peer back-off would otherwise keep us isolated for minutes/hours.
            # So BEFORE we pick peers to dial, wipe every back-off timer and re-attempt all known peers
            # in THIS very pass, in parallel up to the thread cap below. is_isolated() is the SAME
            # predicate the ConnectionManager uses to pick the 5s cadence, so the redial-all bypass and
            # the faster dialing always engage together -- the coupling the reverted attempt lacked.
            isolated = self.is_isolated()
            if isolated and self.peer_dict:
                self.app_log.warning(
                    f"ISOLATION RECOVERY: only {self.outbound_count()} outbound connection(s) "
                    f"(<= floor {OUTBOUND_RECOVERY_FLOOR}); clearing all retry back-off and re-dialing "
                    f"every known peer now (ConnectionManager will also poll every "
                    f"{RECOVERY_LOOP_SECONDS}s until reconnected)")
                self.reset_tried(aggressive=True)

            # Optimization: Cache peer_dict for iteration
            current_peers = dict(self.dict_shuffle(self.peer_dict))
            # When isolated, dial recently-responsive peers first so we reconnect to a live one ASAP
            # instead of burning the (capped) parallel dial slots on peers that may be down.
            if isolated:
                current_peers = self._order_by_recency(current_peers)

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

            # Authoritative outbound count: the connection_pool LIST length, the SAME source of truth as
            # is_isolated() above and the ConnectionManager cadence -- never the _connection_pool_set,
            # which can diverge under churn (that desync is exactly what broke the reverted attempt).
            pool_size = self.outbound_count()
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