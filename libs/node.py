import threading
import queue
import sys

class Node():

    def __init__(self):
        self.logger = None
        self.db_lock = threading.Lock()
        self.app_version = None
        self.startup_time = None
        self.version_allow = None
        self.hdd_block = None #interferes with block flows, careful
        self.last_block = None #interferes with block flows, careful
        self.plugin_manager = None
        self.peers = None
        self.IS_STOPPING = False
        self.apihandler = None
        self.syncing = []
        self.checkpoint = 0

        self.is_testnet = False
        self.is_regnet = False
        self.is_mainnet = False

        self.port = None
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
        self.tor = None
        self.ram = None
        self.version_allow = None
        self.reveal_address = None
        self.terminal_output = None
        self.egress = None
        self.genesis = None
        self.last_block_ago = None
        self.last_block_timestamp = 0
        self.accept_peers = True
        self.difficulty = [0,0,0,0,0,0,0,0]
        self.ledger_temp = None
        self.hyper_temp = None
        self.q = queue.Queue()
        self.py_version= int(str(sys.version_info.major) + str(sys.version_info.minor) + str(sys.version_info.micro))
        self.keys = None