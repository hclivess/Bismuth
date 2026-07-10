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

# Peer discovery / announcement.
#   PEERS_ANNOUNCE_MAX  - upper bound on how many peers we gossip in one `hello`/`peersget` exchange.
#     The old code shipped the ENTIRE raw peers.txt to any connector — unbounded bandwidth and it
#     leaks the node's full known-peer set. A bounded random sample still propagates the network well
#     (peers gossip continuously) without either problem.
#   INBOUND_CANDIDATES_MAX - cap on the set of not-yet-validated inbound peer IPs awaiting a probe, so
#     a flood of inbound connections can't grow it without bound.
PEERS_ANNOUNCE_MAX = 50
INBOUND_CANDIDATES_MAX = 500


class Peers(PeersStorageMixin, PeersPoolMixin, PeersConsensusMixin, PeersAccessMixin, PeersReputationMixin):
    """The peers manager. A thread safe peers manager"""

    __slots__ = ('app_log','config','logstats','node','peersync_lock','peers_lock','startup_time','reset_time','warning_list','stats',
                 'connection_pool','peer_opinion_dict','consensus_percentage','consensus',
                 'tried','peer_dict','peerfile','suggested_peerfile','banlist','whitelist','ban_threshold',
                 'ip_to_mainnet', 'peers', 'accept_peers', 'peerlist_updated', '_warning_counts',
                 '_connection_pool_set', '_c_class_cache', '_peer_dict_cache', '_cache_timestamp',
                 '_reputation', '_inbound_candidates')

    def __init__(self, app_log, config=None, logstats=True, node=None):
        self.app_log = app_log
        self.config = config
        self.logstats = logstats
        self.peersync_lock = threading.Lock()
        # ONE reentrant lock guarding every shared mutable peer collection (peer_dict, connection_pool /
        # _connection_pool_set, tried, peer_opinion_dict, banlist, _warning_counts/warning_list,
        # ip_to_mainnet, _reputation). These are read+written concurrently by the ConnectionManager
        # thread, every inbound handle() thread, and every outbound worker thread; before this the class
        # claimed "thread safe" but had only peersync_lock, and set/dict iterations (can_connect_to over
        # _connection_pool_set, consensus/reputation tallies over peer_opinion_dict/_reputation) could
        # raise "changed size during iteration" mid-consensus. RLock because the mutators nest within one
        # thread (consensus_add -> penalize -> warning -> banlist; append_client -> del_try). INVARIANT:
        # never hold this across network or file I/O — probes stay in _probe_many (unlocked) and disk is
        # read BEFORE locking; the lock only ever wraps in-memory dict/list ops, so it can't stall.
        # Ordering vs peersync_lock: peersync takes peersync_lock THEN peers_lock (for the final
        # peer_dict mutation) — never the reverse — so the two cannot deadlock.
        self.peers_lock = threading.RLock()
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
        self._inbound_candidates = set()  # inbound peer IPs awaiting async probe-validation (discovery)

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

    def _own_public_address(self):
        """Our own dialable address as (ip, port) IF a real public IP is configured, else None. Announcing
        127.0.0.1/localhost/empty is useless (peers can't dial it) and actively harmful (it self-dials), so
        we announce ourselves ONLY when node_ip is set to a routable address. This is the TODO workaround in
        node.py handle(): a node that only ever receives inbound connections is otherwise never gossiped and
        the network never learns to dial it back."""
        ip = str(getattr(self.config, "node_ip", "") or "")
        if ip in ("", "127.0.0.1", "localhost", "0.0.0.0"):
            return None
        return ip, str(self.config.port)

    def peers_to_announce(self):
        """The peer map to gossip in a `hello`/`peersget` reply, as a JSON string {ip:port}.

        Replaces sending the raw, unbounded peers.txt: we send at most PEERS_ANNOUNCE_MAX peers (a random
        sample when we know more, so different connectors learn different peers and the whole set still
        propagates), and we INCLUDE our own public ip:port when configured so connectors learn to dial us
        back. Bounded => no full-peerlist leak and no unbounded payload."""
        import json
        with self.peers_lock:
            items = list(self.peer_dict.items())
        random.shuffle(items)
        sample = dict(items[:PEERS_ANNOUNCE_MAX])
        own = self._own_public_address()
        if own is not None:
            sample[own[0]] = own[1]         # always advertise ourselves, even if sampled out
        return json.dumps(sample)

    def record_inbound_peer(self, peer_ip):
        """Remember an IP that connected to us so we can try to dial it BACK later (peer discovery).

        We only see the inbound peer's source IP, not its listening port, so we stash the bare IP here —
        instantly, no probe (the connection handler must not block) — and the maintenance loop later probes
        it on the default port via promote_inbound_candidates(). Guarded: skip ourselves, banned, and peers
        we already know; bounded by INBOUND_CANDIDATES_MAX so an inbound flood can't grow it without limit."""
        if not peer_ip or peer_ip in ("127.0.0.1", "localhost", getattr(self.config, "node_ip", None)):
            return
        with self.peers_lock:
            if (peer_ip in self.peer_dict or peer_ip in self.banlist
                    or len(self._inbound_candidates) >= INBOUND_CANDIDATES_MAX):
                return
            self._inbound_candidates.add(peer_ip)

    def _drain_inbound_candidates(self):
        """Atomically take and clear the pending inbound IPs, as a {ip: default_port} dict to probe."""
        with self.peers_lock:
            drained = list(self._inbound_candidates)
            self._inbound_candidates.clear()
        port = 2829 if self.is_testnet else str(self.config.port)
        return {ip: port for ip in drained}

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

            # Snapshot peer_dict UNDER LOCK before iterating: dict_shuffle does list(items()), which
            # raises "dictionary changed size during iteration" if a worker/peersync thread mutates
            # peer_dict concurrently. We iterate the private copy, so spawning dial threads below can't
            # race the snapshot.
            with self.peers_lock:
                current_peers = self.dict_shuffle(dict(self.peer_dict))
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
                _suggested = self.peers_get(self.suggested_peerfile)   # disk read OUTSIDE the lock
                with self.peers_lock:
                    self.peer_dict.update(_suggested)

            if pool_size < self.config.nodes_ban_reset and time_since_start > 15:
                self.app_log.warning(f"Only {pool_size} connections active, resetting banlist")
                with self.peers_lock:
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
                with self.peers_lock:
                    self.banlist[:] = self.config.banlist
                    self.warning_list.clear()
                    self._warning_counts.clear()
                self.reset_tried()
                self.reset_time = time()

            self.app_log.warning("Status: Testing peers")
            _known = self.peers_get(self.peerfile)                 # disk read OUTSIDE the lock
            with self.peers_lock:
                self.peer_dict.update(_known)

            # Peer discovery from INBOUND connections: probe the IPs that dialed us (on the default port,
            # our best guess for their listener) with a full version handshake; the reachable Bismuth nodes
            # get persisted to suggested_peers and folded into peer_dict, so we learn dialable peers we'd
            # otherwise never know. Probing is concurrent + non-blocking, so this can't stall the loop.
            inbound = self._drain_inbound_candidates()
            if inbound:
                self.app_log.info(f"Discovery: probing {len(inbound)} inbound peer candidate(s)")
                self.peers_test(self.suggested_peerfile, inbound, strict=True)
                _sug = self.peers_get(self.suggested_peerfile)
                with self.peers_lock:
                    self.peer_dict.update(_sug)

            # Testing peers
            self.peers_test(self.suggested_peerfile, self.peer_dict, strict=False)
            self.peers_test(self.peerfile, self.peer_dict, strict=True)

        except Exception as e:
            self.app_log.warning(f"Status: peers client loop skipped due to error: {e}")

    def status_log(self):
        """Prints the peers part of the node status"""
        # Snapshot every shared collection UNDER LOCK, then log the copies. repr() of a live dict/list in
        # an f-string iterates it and would raise "changed size during iteration" if a peer thread mutates
        # mid-format; and we must not hold peers_lock across the (slow) log I/O.
        with self.peers_lock:
            banlist = list(self.banlist)
            tried = dict(self.tried)
            connection_pool = list(self.connection_pool)
            peer_opinion = dict(self.peer_opinion_dict)
            known_count = len(self.peer_dict)
        if banlist:
            self.app_log.warning(f"Status: Banlist: {banlist}")
            self.app_log.warning(f"Status: Banlist Count : {len(banlist)}")
        if self.whitelist:
            self.app_log.warning(f"Status: Whitelist: {self.whitelist}")

        self.app_log.warning(f"Status: Known Peers: {known_count}")
        self.app_log.info(f"Status: Tried: {tried}")
        self.app_log.info(f"Status: Tried Count: {len(tried)}")
        self.app_log.info(f"Status: List of Outbound connections: {connection_pool}")
        self.app_log.warning(f"Status: Number of Outbound connections: {len(connection_pool)}")
        if self.consensus:
            self.app_log.warning(f"Status: Consensus height: {self.consensus} = {self.consensus_percentage}%")
            self.app_log.warning(f"Status: Last block opinion: {peer_opinion}")
            self.app_log.warning(f"Status: Total number of nodes: {len(peer_opinion)}")