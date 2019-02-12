import threading
import time
from worker import worker

class ConnectionManager (threading.Thread):
	def __init__(self, node, mp):
		threading.Thread.__init__(self)
		self.node = node
		self.db_lock = node.db_lock
		self.logger = node.logger
		self.mp = mp

	def run(self):
		self.connection_manager()

	def connection_manager(self):
		self.logger.app_log.warning("Status: Starting connection manager")
		until_purge = 0

		while not self.node.IS_STOPPING or self.db_lock.locked():
			# dict_keys = peer_dict.keys()
			# random.shuffle(peer_dict.items())
			if until_purge == 0:
				# will purge once at start, then about every hour (120 * 30 sec)
				self.mp.MEMPOOL.purge()
				until_purge = 120

			until_purge -= 1

			# peer management
			
			if not self.node.is_regnet:
				# regnet never tries to connect
				self.node.peers.client_loop(self.node, target = worker)

			self.logger.app_log.warning(f"Status: Threads at {threading.active_count()} / {self.node.thread_limit_conf}")
			self.logger.app_log.info(f"Status: Syncing nodes: {self.node.syncing}")
			self.logger.app_log.info(f"Status: Syncing nodes: {len(self.node.syncing)}/3")

			# Status display for Peers related info
			self.node.peers.status_log()
			self.mp.MEMPOOL.status()

			if self.node.last_block_ago:
				self.node.last_block_ago = time.time() - int(self.node.last_block_timestamp)
				self.logger.app_log.warning(f"Status: Last block {self.node.last_block} was generated {'%.2f' % (self.node.last_block_ago / 60) } minutes ago")
			# last block
			# status Hook
			uptime = int(time.time() - self.node.startup_time)

			status = {"protocolversion": self.node.version, "walletversion": self.node.app_version, "testnet": self.node.is_testnet,
					  # config data
					  "blocks": self.node.last_block, "timeoffset": 0, "connections": self.node.peers.consensus_size,
					  "difficulty": self.node.difficulty[0],  # live status, bitcoind format
					  "threads": threading.active_count(), "uptime": uptime, "consensus": self.node.peers.consensus,
					  "consensus_percent": self.node.peers.consensus_percentage,
					  "last_block_ago": self.node.last_block_ago}  # extra data
			if self.node.is_regnet:
				status['regnet'] = True
			self.node.plugin_manager.execute_action_hook('status', status)
			# end status hook

			if self.node.peerfile_suggested:  # if it is not empty
				try:
					self.node.peers.peers_dump(self.node.peerfile_suggested, self.node.peers.peer_dict)
				except Exception as e:
					self.logger.app_log.warning(f"There was an issue saving peers ({e}), skipped")
					pass

			# logger.app_log.info(threading.enumerate() all threads)
			time.sleep(30)
			"""
			for i in range(30):
				# faster stop
				if not node.IS_STOPPING:
					time.sleep(1)
			"""