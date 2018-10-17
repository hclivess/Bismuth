import os.path as path


class Get:

    # "param_name":["type"] or "param_name"=["type","property_name"]
    vars={
        "port":["str"],
        "verify":["bool","verify_conf"],
        "version":["str","version_conf"],
        "testnet":["bool"],
        "regnet":["bool"],
        "version":["str","version_conf"],
        "version_allow":["list"],
        "thread_limit":["int","thread_limit_conf"],
        "rebuild_db":["bool","rebuild_db_conf"],
        "debug":["bool","debug_conf"],
        "purge":["bool","purge_conf"],
        "pause":["int","pause_conf"],
        "ledger_path":["str","ledger_path_conf"],
        "hyper_path":["str","hyper_path_conf"],
        "hyper_recompress":["bool","hyper_recompress_conf"],
        "full_ledger":["bool","full_ledger_conf"],
        "ban_threshold":["int"],
        "tor":["bool","tor_conf"],
        "debug_level":["str","debug_level_conf"],
        "allowed":["str","allowed_conf"],
        "pool_ip":["str","pool_ip_conf"],
        "miner_sync":["bool","sync_conf"],
        "mining_threads":["str","mining_threads_conf"],
        "diff_recalc":["int","diff_recalc_conf"],
        "mining_pool":["bool","pool_conf"],
        "pool_address":["str","pool_address_conf"],
        "ram":["bool","ram_conf"],
        "pool_percentage":["int","pool_percentage_conf"],
        "node_ip":["str","node_ip"],
        "light_ip":["list"],
        "reveal_address":["bool"],
        "accept_peers":["bool"],
        "banlist":["list"],
        "whitelist":["list"],
        "nodes_ban_reset":["int"],
        "mempool_allowed": ["list"],
        "terminal_output": ["bool"],
        "gui_scaling": ["str"],
        "mempool_ram_conf": ["bool"],
        "egress": ["bool"]
    }

    # Optional default values so we don't bug if they are not in the config.
    # For compatibility
    defaults = {
        "testnet": False,
        "regnet": False
    }

    def load_file(self,filename):
        #print("Loading",filename)
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
                elif params[0] == "bool":
                    if right.lower() in ["false", "0", "", "no"]:
                        right = False
                    else:
                        right = True

                else:
                    # treat as "str"
                    pass
                if len(params)>1:
                    # deal with properties that do not match the config name.
                    left = params[1]
                setattr(self,left,right)
        # Default genesis to keep compatibility
        self.genesis_conf = "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed"
        for key, default in self.defaults.items():
            if key not in self.__dict__:
                setattr(self, key, default)

        #print(self.__dict__)

    def read(self):
        # first of all, load from default config so we have all needed params
        self.load_file("config.txt")
        # then override with optional custom config
        if path.exists("config_custom.txt"):
            self.load_file("config_custom.txt")
