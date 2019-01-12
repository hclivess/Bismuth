class Node():

    def __init__(self):
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
        self.peerlist = None
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
        self.last_block_ago = 0
        self.last_block_timestamp = 0
        self.accept_peers = True



class Logger():
    def __init__(self):
        self.app_log = None

class Database():
    def __init__(self):
        self.to_ram = None
        self.tr = None

        self.conn = None
        self.c = None

        self.hdd = None
        self.h = None

        self.hdd2 = None
        self.h2 = None

        self.hdd3 = None
        self.h3 = None

        self.index = None
        self.index_cursor = None

        self.source_db = None
        self.sc = None


class Keys():
    def __init__(self):
        self.public_key_readable = None
        self.public_key_hashed = None
        self.address = None
        self.keyfile = None

