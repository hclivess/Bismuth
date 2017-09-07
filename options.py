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
                self.hyper_recompress_conf = line.lstrip('hyper_recompress=')
            if "warning_list_limit=" in line:
                self.warning_list_limit_conf = int(line.lstrip('warning_list_limit='))
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
            if "pool_percentage=" in line:
                self.pool_percentage_conf = int(line.lstrip('pool_percentage='))
            if "node_ip=" in line:
                self.node_ip_conf = line.lstrip('node_ip=')