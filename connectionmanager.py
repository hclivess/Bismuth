import threading
import time
from worker import worker


class ConnectionManager (threading.Thread):
    def __init__(self, node, mp):
        threading.Thread.__init__(self, name="ConnectionManagerThread")
        self.node = node
        self.mp = mp

    def run(self):

        self.connection_manager()

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
                if not self.node.is_regnet:
                    # regnet never tries to connect
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
                # time.sleep(30)
                for i in range(30):
                    # faster stop
                    if not self.node.IS_STOPPING:
                        time.sleep(1)
            except Exception as e:
                self.node.logger.app_log.warning(f"Error in connection manger ({e})")
