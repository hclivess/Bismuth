import socketserver, connections, time, options, log, sqlite3, ast, socks, hashlib
(port, genesis_conf, verify_conf, version_conf, thread_limit_conf, rebuild_db_conf, debug_conf, purge_conf, pause_conf, ledger_path_conf, hyperblocks_conf, warning_list_limit_conf, tor_conf, debug_level_conf, allowed, mining_ip_conf, sync_conf, mining_threads_conf, diff_recalc_conf, pool_conf, pool_address, ram_conf) = options.read()
app_log = log.log("pool.log",debug_level_conf)

def diffget():
    s = socks.socksocket()
    if tor_conf == 1:
        s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
    s.connect((mining_ip_conf, int(port)))  # connect to local node

    connections.send(s, "diffget", 10)
    diff = float(connections.receive(s, 10))
    s.close()
    return diff

def bin_convert(string):
    return ''.join(format(ord(x), 'b') for x in string)

class MyTCPHandler(socketserver.BaseRequestHandler):

    def handle(self):
        peer_ip = self.request.getpeername()[0]

        data = connections.receive(self.request, 10)
        app_log.info("Received: {} from {}".format(data, peer_ip))  # will add custom ports later

        if data == 'diffget':
            diff = diffget()
            connections.send(self.request, diff, 10)


        elif data == "block":  # from miner to node

            app_log.warning("Received a block from miner {}".format(peer_ip))
            # receive block
            block_send = ast.literal_eval(connections.receive(self.request, 10))
            print (block_send)
            nonce = (block_send[-1][7])

            # check difficulty
            diff = int(diffget())
            app_log.info("Calculated difficulty: {}".format(diff))
            # check difficulty

            # sock
            s = socks.socksocket()
            if tor_conf == 1:
                s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
            s.connect(("127.0.0.1", int(port)))  # connect to local node
            # sock

            # get last hash
            connections.send(s, "blocklast", 10)
            blocklast = ast.literal_eval(connections.receive(s, 10))
            db_block_hash = blocklast[7]
            # get last hash


            print (nonce)
            print (pool_address)
            print (db_block_hash)

            mining_hash = bin_convert(hashlib.sha224((pool_address + nonce + db_block_hash).encode("utf-8")).hexdigest())
            mining_condition = bin_convert(db_block_hash)[0:diff]

            if mining_condition in mining_hash:
                app_log.info("Difficulty requirement satisfied")
                app_log.warning("Sending block to node {}".format(peer_ip))

                connections.send(s, "block", 10)
                connections.send(s, block_send, 10)

            else:
                mining_condition = bin_convert(db_block_hash)[0:20]

                if mining_condition in mining_hash:
                    app_log.info("Difficulty requirement satisfied for saving shares")
                else:
                    app_log.info("Difficulty requirement not satisfied for anything")

            s.close()

app_log.warning("Starting up...")

if __name__ == "__main__":
    HOST, PORT = "localhost", 8525

    # Create the server, binding to localhost on port 9999
    server = socketserver.TCPServer((HOST, PORT), MyTCPHandler)

    # Activate the server; this will keep running until you
    # interrupt the program with Ctrl-C
    server.serve_forever()