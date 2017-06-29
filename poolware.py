import socketserver, connections, time, options, log, sqlite3, ast, socks, hashlib, os, random
(port, genesis_conf, verify_conf, version_conf, thread_limit_conf, rebuild_db_conf, debug_conf, purge_conf, pause_conf, ledger_path_conf, hyperblocks_conf, warning_list_limit_conf, tor_conf, debug_level_conf, allowed, mining_ip_conf, sync_conf, mining_threads_conf, diff_recalc_conf, pool_conf, pool_address, ram_conf) = options.read()
app_log = log.log("pool.log",debug_level_conf)

def commit(cursor):
    # secure commit for slow nodes
    passed = 0
    while passed == 0:
        try:
            cursor.commit()
            passed = 1
        except Exception as e:
            app_log.warning("Retrying database execute due to " + str(e))
            time.sleep(random.random())
            pass
            # secure commit for slow nodes

def execute(cursor, what):
    # secure execute for slow nodes
    passed = 0
    while passed == 0:
        try:
            # print cursor
            # print what

            cursor.execute(what)
            passed = 1
        except Exception as e:
            app_log.warning("Retrying database execute due to {}".format(e))
            time.sleep(random.random())
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
        except Exception as e:
            app_log.warning("Retrying database execute due to " + str(e))
            time.sleep(0.1)
            pass
            # secure execute for slow nodes
    return cursor

def diffget(s):
    connections.send(s, "diffget", 10)
    diff = float(connections.receive(s, 10))
    return diff

def bin_convert(string):
    return ''.join(format(ord(x), 'b') for x in string)

if not os.path.exists('shares.db'):
    # create empty mempool
    shares = sqlite3.connect('shares.db')
    shares.text_factory = str
    s = shares.cursor()
    execute(s, ("CREATE TABLE IF NOT EXISTS shares (address, shares, timestamp)"))
    app_log.warning("Created mempool file")
    s.close()
    # create empty mempool

class MyTCPHandler(socketserver.BaseRequestHandler):

    def handle(self):
        peer_ip = self.request.getpeername()[0]

        data = connections.receive(self.request, 10)
        app_log.warning("Received: {} from {}".format(data, peer_ip))  # will add custom ports later

        #if data == 'diffget':
        #    diff = diffget()
        #    connections.send(self.request, diff, 10)


        if data == "block":  # from miner to node

            # sock
            s = socks.socksocket()
            if tor_conf == 1:
                s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
            s.connect(("127.0.0.1", int(port)))  # connect to local node,
            # sock


            # receive block
            miner_address = connections.receive(self.request, 10)
            app_log.warning("Received a block from miner {} ({})".format(peer_ip,miner_address))

            block_send = ast.literal_eval(connections.receive(self.request, 10))
            nonce = (block_send[-1][7])

            #print(nonce)
            #print(block_send)
            #print(miner_address)

            # check difficulty
            app_log.warning("Asking node for difficulty")
            diff = int(diffget(s))
            app_log.warning("Calculated difficulty: {}".format(diff))
            # check difficulty

            app_log.warning("Asking node for last block")

            # get last block
            connections.send(s, "blocklast", 10)
            blocklast = ast.literal_eval(connections.receive(s, 10))
            db_block_hash = blocklast[7]
            # get last block

            app_log.warning("Last Hash: {}".format(db_block_hash))

            mining_hash = bin_convert(hashlib.sha224((pool_address + nonce + db_block_hash).encode("utf-8")).hexdigest())
            mining_condition = bin_convert(db_block_hash)[0:diff]

            if mining_condition in mining_hash:
                app_log.warning("Difficulty requirement satisfied for mining")
                app_log.warning("Sending block to node {}".format(peer_ip))

                connections.send(s, "block", 10)
                connections.send(s, block_send, 10)


            mining_condition = bin_convert(db_block_hash)[0:37] #floor set by pool
            if mining_condition in mining_hash:
                app_log.warning("Difficulty requirement satisfied for saving shares")
                timestamp = '%.2f' % time.time()

                shares = sqlite3.connect('shares.db')
                shares.text_factory = str
                s = shares.cursor()

                s.execute("INSERT INTO shares VALUES (?,?,?)", (str(miner_address), str(1), timestamp))
                shares.commit()
                s.close()

            else:
                app_log.warning("Difficulty requirement not satisfied for anything")

            s.close()

app_log.warning("Starting up...")

if __name__ == "__main__":
    HOST, PORT = "0.0.0.0", 8525

    # Create the server, binding to localhost on port 9999
    server = socketserver.TCPServer((HOST, PORT), MyTCPHandler)

    # Activate the server; this will keep running until you
    # interrupt the program with Ctrl-C
    server.serve_forever()