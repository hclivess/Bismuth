"""Outbound-connection manager: keeps the node connected to enough peers.

Runs as a background thread that maintains the node's set of outbound peer
connections. It periodically consults the peer list, and for each peer that
should be connected but isn't, spawns a ``worker`` thread (respecting the
configured connection/thread limit) to sync with that peer. By topping the
connection pool back up as workers exit, it keeps the node participating in the
network. It contains no consensus logic -- it only manages connectivity.
"""

import threading
import time
from worker import worker
# The isolation floor and both loop cadences live in peershandler so the dial-decision (client_loop)
# and the loop-timing (here) share ONE source of truth. Importing them -- rather than re-deriving the
# threshold from a second counter -- is what keeps the faster cadence coupled to the redial-all bypass;
# decoupling them on two different counters is precisely what regressed the reverted attempt.
from peershandler import NORMAL_LOOP_SECONDS, RECOVERY_LOOP_SECONDS


class ConnectionManager (threading.Thread):
    def __init__(self, node, mp):
        threading.Thread.__init__(self, name="ConnectionManagerThread")
        self.node = node
        self.mp = mp

    def run(self):

        self.connection_manager()

    def _dials_out(self):
        """Does this node dial peers? Mainnet/testnet always; regnet only under the multi-node harness
        (node.regnet_peering, doc/47)."""
        return (not self.node.is_regnet) or bool(getattr(self.node, "regnet_peering", False))

    def _loop_seconds(self):
        """Seconds to sleep before the next maintenance/dial pass.

        Normally NORMAL_LOOP_SECONDS; while this node dials out (not regnet) AND is isolated it shortens
        to RECOVERY_LOOP_SECONDS so the redial-all pass repeats within seconds. The isolation test is the
        SAME peers.is_isolated() predicate (over the SAME len(connection_pool) counter) that client_loop
        uses to wipe back-off and re-dial every peer -- so the faster cadence and the redial-all bypass
        are coupled on one condition and can never get out of sync (the reverted attempt's regression).
        Regnet never dials, so it always gets the full interval and never busy-spins."""
        if self._dials_out() and self.node.peers.is_isolated():
            return RECOVERY_LOOP_SECONDS
        return NORMAL_LOOP_SECONDS

    def connection_manager(self):
        self.node.logger.app_log.warning("Status: Starting connection manager")
        until_purge = 0

        while not self.node.IS_STOPPING:
            # one loop every 30 sec
            try:
                # dict_keys = peer_dict.keys()
                # random.shuffle(peer_dict.items())
                if until_purge <= 0:
                    # will purge once at start, then about every half hour (60 * 30 sec)
                    self.mp.MEMPOOL.purge()
                    until_purge = 60
                until_purge -= 1

                # peer management
                if self._dials_out():
                    # regnet never tries to connect (unless the multi-node harness enables regnet_peering)
                    self.node.peers.client_loop(self.node, this_target=worker)
                self.node.logger.app_log.warning(f"Status: Threads at {threading.active_count()} / {self.node.thread_limit}")
                self.node.logger.app_log.info(f"Status: Syncing nodes: {self.node.syncing}")
                self.node.logger.app_log.info(f"Status: Syncing nodes: {len(self.node.syncing)}/3")

                # Status display for Peers related info
                self.node.peers.status_log()
                self.mp.MEMPOOL.status()
                # last block
                if self.node.last_block_ago:
                    self.node.last_block_ago = time.time() - int(self.node.last_block_timestamp)
                    self.node.logger.app_log.warning(f"Status: Last block {self.node.last_block} was generated "
                                                f"{'%.2f' % (self.node.last_block_ago / 60) } minutes ago")
                # status Hook
                uptime = int(time.time() - self.node.startup_time)
                status = {"protocolversion": self.node.version,
                          "walletversion": self.node.app_version,
                          "testnet": self.node.is_testnet,
                          # config data
                          "blocks": self.node.last_block,
                          "timeoffset": 0,
                          "connections": self.node.peers.consensus_size,
                          "difficulty": self.node.difficulty[0],  # live status, bitcoind format
                          "threads": threading.active_count(),
                          "uptime": uptime,
                          "consensus": self.node.peers.consensus,
                          "consensus_percent": self.node.peers.consensus_percentage,
                          "last_block_ago": self.node.last_block_ago}  # extra data
                if self.node.is_regnet:
                    status['regnet'] = True
                self.node.plugin_manager.execute_action_hook('status', status)
                # end status hook

                # logger.app_log.info(threading.enumerate() all threads)
                # Sleep before the next maintenance/dial pass. _loop_seconds() returns the cadence: the
                # normal 30s, or the 5s recovery cadence while isolated -- coupled to the client_loop
                # redial-all bypass via the one shared is_isolated() predicate (see _loop_seconds). The
                # 1s granularity keeps shutdown responsive.
                for i in range(self._loop_seconds()):
                    # faster stop
                    if not self.node.IS_STOPPING:
                        time.sleep(1)
            except Exception as e:
                self.node.logger.app_log.warning(f"Error in connection manger ({e})")
