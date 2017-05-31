import SocketServer, connections, time, options, log, sqlite3
(port, genesis_conf, verify_conf, version_conf, thread_limit_conf, rebuild_db_conf, debug_conf, purge_conf, pause_conf, ledger_path_conf, hyperblocks_conf, warning_list_limit_conf, tor_conf, debug_level_conf, allowed) = options.read()
app_log = log.log("pool.log",debug_level_conf)

def diffget():
    conn = sqlite3.connect(ledger_path_conf)
    c = conn.cursor()

    execute(c, ("SELECT timestamp,block_height FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
    result = c.fetchall()
    db_timestamp_last = float(result[0][0])

    # calculate difficulty
    execute_param(c, ("SELECT block_height FROM transactions WHERE CAST(timestamp AS INTEGER) > ? AND reward != 0"), (db_timestamp_last - 1800,))  # 1800=30 min
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


def difftest(block, diff):

    pass

def execute(cursor, what):
    # secure execute for slow nodes
    passed = 0
    while passed == 0:
        try:
            # print cursor
            # print what

            cursor.execute(what)
            passed = 1
        except Exception, e:
            app_log.info("Retrying database execute due to {}".format(e))
            time.sleep(0.1)
            pass
            # secure execute for slow nodes
    return cursor


def execute_param(cursor, what, param):
    # secure execute for slow nodes
    passed = 0
    while passed == 0:
        try:
            # print cursor
            # print what
            cursor.execute(what, param)
            passed = 1
        except Exception, e:
            app_log.info("Retrying database execute due to " + str(e))
            time.sleep(0.1)
            pass
            # secure execute for slow nodes
    return cursor

class MyTCPHandler(SocketServer.BaseRequestHandler):

    def handle(self):
        peer_ip = self.request.getpeername()[0]

        data = connections.receive(self.request, 10)
        app_log.info("Incoming: Received: {} from {}".format(data, peer_ip))  # will add custom ports later

        if data == 'diffget':
            diff = diffget()
            connections.send(self.request, diff, 10)


        elif data == "block" and (peer_ip in allowed or "any" in allowed):  # from miner

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
            # receive theirs

if __name__ == "__main__":
    HOST, PORT = "localhost", 8525

    # Create the server, binding to localhost on port 9999
    server = SocketServer.TCPServer((HOST, PORT), MyTCPHandler)

    # Activate the server; this will keep running until you
    # interrupt the program with Ctrl-C
    server.serve_forever()