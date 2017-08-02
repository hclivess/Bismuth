import os.path as path
def read():
    if not path.exists("config_custom.txt"):
        lines = [line.rstrip('\n') for line in open('config.txt')]
    else:
        lines = [line.rstrip('\n') for line in open('config_custom.txt')]

    for line in lines:
        if "port=" in line:
            port = line.lstrip('port=')
        if "genesis=" in line:
            genesis_conf = line.lstrip('genesis=')
        if "verify=" in line:
            verify_conf = int(line.lstrip('verify='))
        if "version=" in line:
            version_conf = line.lstrip('version=')
        if "thread_limit=" in line:
            thread_limit_conf = int(line.lstrip('thread_limit='))
        if "rebuild_db=" in line:
            rebuild_db_conf = int(line.lstrip('rebuild_db='))
        if "debug=" in line:
            debug_conf = int(line.lstrip('debug='))
        if "purge=" in line:
            purge_conf = int(line.lstrip('purge='))
        if "pause=" in line:
            pause_conf = line.lstrip('pause=')
        if "ledger_path=" in line:
            ledger_path_conf = line.lstrip('ledger_path=')
        if "hyperblocks=" in line:
            hyperblocks_conf = int(line.lstrip('hyperblocks='))
        if "warning_list_limit=" in line:
            warning_list_limit_conf = int(line.lstrip('warning_list_limit='))
        if "tor=" in line:
            tor_conf = int(line.lstrip('tor='))
        if "debug_level=" in line:
            debug_level_conf = line.lstrip('debug_level=')
        if "allowed=" in line:
            allowed_conf = line.lstrip('allowed=')
        if "pool_ip=" in line:
            pool_ip_conf = line.lstrip("pool_ip=")
        if "miner_sync=" in line:
            sync_conf = int(line.lstrip('miner_sync='))
        if "mining_threads=" in line:
            mining_threads_conf = line.lstrip('mining_threads=')
        if "diff_recalc=" in line:
            diff_recalc_conf = line.lstrip('diff_recalc=')
        if "mining_pool=" in line:
            pool_conf = int(line.lstrip('mining_pool='))
        if "pool_address=" in line:
            pool_address_conf = line.lstrip('pool_address=')
        if "ram=" in line:
            ram_conf = int(line.lstrip('ram='))
        if "pool_percentage=" in line:
            pool_percentage_conf = int(line.lstrip('pool_percentage='))
        if "node_ip=" in line:
            node_ip_conf = line.lstrip('node_ip=')

    return port, genesis_conf, verify_conf, version_conf, thread_limit_conf, rebuild_db_conf, debug_conf, purge_conf, pause_conf, ledger_path_conf, hyperblocks_conf, warning_list_limit_conf, tor_conf, debug_level_conf, allowed_conf, pool_ip_conf, sync_conf, mining_threads_conf, diff_recalc_conf, pool_conf, pool_address_conf, ram_conf, pool_percentage_conf, node_ip_conf