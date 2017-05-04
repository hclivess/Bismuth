def read():
    lines = [line.rstrip('\n') for line in open('config.txt')]

    for line in lines:
        if "port=" in line:
            port = line.strip('port=')
        if "genesis=" in line:
            genesis_conf = line.strip('genesis=')
        if "verify=" in line:
            verify_conf = int(line.strip('verify='))
        if "version=" in line:
            version_conf = line.strip('version=')
        if "thread_limit=" in line:
            thread_limit_conf = int(line.strip('thread_limit='))
        if "rebuild_db=" in line:
            rebuild_db_conf = int(line.strip('rebuild_db='))
        if "debug=" in line:
            debug_conf = int(line.strip('debug='))
        if "purge=" in line:
            purge_conf = int(line.strip('purge='))
        if "pause=" in line:
            pause_conf = line.strip('pause=')
        if "ledger_path=" in line:
            ledger_path_conf = line.strip('ledger_path=')
        if "hyperblocks=" in line:
            hyperblocks_conf = int(line.strip('hyperblocks='))
        if "warning_list_limit=" in line:
            warning_list_limit_conf = int(line.strip('warning_list_limit='))
        if "tor=" in line:
            tor_conf = int(line.strip('tor='))
        if "debug_level=" in line:
            debug_level_conf = line.strip('debug_level=')

    return port, genesis_conf, verify_conf, version_conf, thread_limit_conf, rebuild_db_conf, debug_conf, purge_conf, pause_conf, ledger_path_conf, hyperblocks_conf, warning_list_limit_conf, tor_conf, debug_level_conf