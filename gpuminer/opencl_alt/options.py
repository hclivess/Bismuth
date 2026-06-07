import os.path as path

class Get:
    def read(self):
        if not path.exists("config_custom.txt"):
            lines = [line.rstrip('\n') for line in open('config.txt')]
        else:
            lines = [line.rstrip('\n') for line in open('config_custom.txt')]

        for line in lines:
            if "port=" in line:
                self.port = line.lstrip('port=')
            if "genesis=" in line:
                self.genesis_conf = line.lstrip('genesis=')
            if "verify=" in line:
                self.verify_conf = int(line.lstrip('verify='))
            if "version=" in line:
                self.version_conf = line.lstrip('version=')
            if "version_allow=" in line:
                self.version_allow = line.lstrip('version_allow=').split(",")
            if "thread_limit=" in line:
                self.thread_limit_conf = int(line.lstrip('thread_limit='))
            if "rebuild_db=" in line:
                self.rebuild_db_conf = int(line.lstrip('rebuild_db='))
            if "debug=" in line:
                self.debug_conf = int(line.lstrip('debug='))
            if "purge=" in line:
                self.purge_conf = int(line.lstrip('purge='))
            if "pause=" in line:
                self.pause_conf = line.lstrip('pause=')
            if "ledger_path=" in line:
                self.ledger_path_conf = line.lstrip('ledger_path=')
            if "hyper_path=" in line:
                self.hyper_path_conf = line.lstrip('hyper_path=')
            if "hyper_recompress=" in line:
                self.hyper_recompress_conf = int(line.lstrip('hyper_recompress='))
            if "full_ledger=" in line:
                self.full_ledger_conf = int(line.lstrip('full_ledger='))
            if "ban_threshold=" in line:
                self.ban_threshold = int(line.lstrip('ban_threshold='))
            if "tor=" in line:
                self.tor_conf = int(line.lstrip('tor='))
            if "debug_level=" in line:
                self.debug_level_conf = line.lstrip('debug_level=')
            if "allowed=" in line:
                self.allowed_conf = line.lstrip('allowed=')
            if "pool_ip=" in line:
                self.pool_ip_conf = line.lstrip("pool_ip=")
            if "miner_sync=" in line:
                self.sync_conf = int(line.lstrip('miner_sync='))
            if "mining_threads=" in line:
                self.mining_threads_conf = line.lstrip('mining_threads=')
            if "diff_recalc=" in line:
                self.diff_recalc_conf = int(line.lstrip('diff_recalc='))
            if "mining_pool=" in line:
                self.pool_conf = int(line.lstrip('mining_pool='))
            if "pool_address=" in line:
                self.pool_address_conf = line.lstrip('pool_address=')
            if "ram=" in line:
                self.ram_conf = int(line.lstrip('ram='))
            if "opencl_hash_count=" in line:
                self.opencl_hash_count_conf = int(line.lstrip('opencl_hash_count='))
            if "opencl_timeout=" in line:
                self.opencl_timeout_conf = int(line.lstrip('opencl_timeout='))
            if "opencl_thread_multiplier=" in line:
                self.opencl_thread_multiplier_conf = int(line.lstrip('opencl_thread_multiplier='))
            if "opencl_full_check=" in line:
                self.opencl_full_check_conf= int(line.lstrip('opencl_full_check='))
            if "opencl_disable_device=" in line:
                str = line.lstrip('opencl_disable_device=')
                self.opencl_disable_device_conf = [int(d.strip()) for d in str.split(",") if len(d.strip()) > 0 ]
            if "pool_percentage=" in line:
                self.pool_percentage_conf = int(line.lstrip('pool_percentage='))
            if "node_ip=" in line:
                self.node_ip_conf = line.lstrip('node_ip=')
            if "light_ip=" in line:
                self.light_ip_conf = line.lstrip('light_ip=')
            if "reveal_address=" in line:
                self.reveal_address = line.lstrip('reveal_address=')
            if "accept_peers=" in line:
                self.accept_peers = line.lstrip('accept_peers=')
            if "banlist=" in line:
                self.banlist = line.lstrip('banlist=').split(",")
            if "nodes_ban_reset=" in line:
                self.nodes_ban_reset = int(line.lstrip('nodes_ban_reset='))
            if "whitelist" in line:
                self.whitelist = line.lstrip('whitelist=')
