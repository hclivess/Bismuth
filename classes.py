import threading

class Node():

    def __init__(self):
        self.logger = None
        self.db_lock = threading.Lock()
        self.app_version = None
        self.startup_time = None
        self.version_allow = None
        self.hdd_block = None
        self.last_block = None
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
        self.full_ledger = None
        self.hyper_recompress_conf = True
        self.hyper_path_conf = None
        self.ledger_path_conf = None
        self.ledger_ram_file = None
        self.peerfile = None
        self.peerfile_suggested = None
        self.index_db = None
        self.version = None
        self.version_allow = None

    
        self.version = None
        self.debug_level = None
        self.port = None
        self.verify_conf = None
        self.thread_limit_conf = None
        self.rebuild_db_conf = None
        self.debug_conf = None
        self.pause_conf = None
        self.ledger_path_conf = None
        self.hyper_path_conf = None
        self.hyper_recompress_conf = None
        self.tor_conf = None
        self.ram_conf = None
        self.version_allow = None
        self.full_ledger = None
        self.reveal_address = None
        self.terminal_output = None
        self.egress = None
        self.genesis_conf = None
        self.last_block_ago = None
        self.last_block_timestamp = 0
        self.accept_peers = True
        self.difficulty = [0,0,0,0,0,0,0,0]
        self.ledger_temp = None
        self.hyper_temp = None

class Client:
    def __init__(self):
        self.banned = False
        self.connected = False

class Logger():
    def __init__(self):
        self.app_log = None

class Keys():
    def __init__(self):
        self.public_key_readable = None
        self.public_key_hashed = None
        self.address = None
        self.keyfile = None

