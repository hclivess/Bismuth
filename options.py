import os.path as path


class Get:
    
    # "param_name":["type"] or "param_name"=["type","property_name"]
    vars={"port":["str"],"genesis":["str","genesis_conf"],"verify":["int","verify_conf"],"version":["str","version_conf"],"version_allow":["list"],
    "thread_limit":["int","thread_limit_conf"],"rebuild_db":["int","rebuild_db_conf"],"debug":["int","debug_conf"],"purge":["int","purge_conf"],
    "pause":["str","pause_conf"],"ledger_path":["str","ledger_path_conf"],"hyper_path":["str","hyper_path_conf"],"hyper_recompress":["int","hyper_recompress_conf"],
    "full_ledger":["int","full_ledger_conf"],"ban_threshold":["int"],"tor":["int","tor_conf"],"debug_level":["str","debug_level_conf"],"allowed":["str","allowed_conf"],
    "pool_ip":["str","pool_ip_conf"],"miner_sync":["int","sync_conf"],"mining_threads":["str","mining_threads_conf"],"diff_recalc":["int","diff_recalc_conf"],
    "mining_pool":["int","pool_conf"],"pool_address":["str","pool_address_conf"],"ram":["int","ram_conf"],"pool_percentage":["int","pool_percentage_conf"],
    "node_ip":["str","node_ip_conf"],"light_ip":["str","light_ip_conf"],"reveal_address":["str"],"accept_peers":["str"],"banlist":["list"],"whitelist":["list"],
    "nodes_ban_reset":["int"]
    }
 
    def load_file(self,filename):
        print("Loading",filename)
        for line in open(filename):
            if '=' in line:
                left,right = map(str.strip,line.rstrip("\n").split("="))
                if not left in self.vars:
                    # Warn for unknown param?
                    continue
                params = self.vars[left]
                if params[0] == "int":
                    right = int(right)
                elif params[0] == "list":
                    right = [item.strip() for item in right.split(",")]
                else:
                    # treat as "str"
                    pass 
                if len(params)>1:
                    # deal with properties that do not match the config name.
                    left = params[1]
                setattr(self,left,right)                
        print(self.__dict__)           
                    
    def read(self):
        # first of all, load from default config so we have all needed params
        self.load_file("config.txt")
        # then override with optional custom config
        if path.exists("config_custom.txt"):
            self.load_file("config_custom.txt")
