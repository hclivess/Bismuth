import sqlite3, options, db, time
(port, genesis_conf, verify_conf, version_conf, thread_limit_conf, rebuild_db_conf, debug_conf, purge_conf, pause_conf, ledger_path_conf, hyperblocks_conf, warning_list_limit_conf, tor_conf, debug_level_conf, allowed) = options.read()

def diffget():
    conn = sqlite3.connect(ledger_path_conf)
    c = conn.cursor()

    db.execute(c, ("SELECT timestamp,block_height FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"),)
    result = c.fetchall()
    db_timestamp_last = float(result[0][0])

    # calculate difficulty
    db.execute_param(c, ("SELECT block_height FROM transactions WHERE CAST(timestamp AS INTEGER) > ? AND reward != 0"), (db_timestamp_last - 1800,))  # 1800=30 min
    blocks_per_30 = len(c.fetchall())

    diff = blocks_per_30 * 2

    # drop diff per minute if over target
    time_drop = time.time()

    drop_factor = 120  # drop 0,5 diff per minute #hardfork

    if time_drop > db_timestamp_last + 120:  # start dropping after 2 minutes
        diff = diff - (time_drop - db_timestamp_last) / drop_factor  # drop 0,5 diff per minute (1 per 2 minutes)

    if time_drop > db_timestamp_last + 300 or diff < 37:  # 5 m lim
        diff = 37  # 5 m lim

    conn.close()

    return diff

def block():
    app_log.warning("Outgoing: Received a block from miner {}".format(peer_ip))
    # receive block
    segments = connections.receive(self.request, 10)
    # app_log.info("Incoming: Combined mined segments: " + segments)

    # check if we have the latest block
    conn = sqlite3.connect(ledger_path_conf)
    conn.text_factory = str
    c = conn.cursor()
    execute(c, ('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1'))
    db_block_height = c.fetchone()[0]
    conn.close()
    # check if we have the latest block

    if len(active_pool) < 5:
        app_log.warning("Outgoing: Mined block ignored, insufficient connections to the network")
    elif int(db_block_height) >= int(max(consensus_blockheight_list)) - 3:
        app_log.warning("Outgoing: Processing block from miner")
        digest_block(segments, self.request, peer_ip)
    # receive theirs
    else:
        app_log.warning("Outgoing: Mined block was orphaned because node was not synced, we are at block {}, should be at least {}".format(db_block_height, int(max(consensus_blockheight_list)) - 3))

