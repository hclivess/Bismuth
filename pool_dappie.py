import SocketServer, threading, options, connections, log, time, socks

# load config
(port, genesis_conf, verify_conf, version_conf, thread_limit_conf, rebuild_db_conf, debug_conf, purge_conf, pause_conf, ledger_path_conf, hyperblocks_conf, warning_list_limit_conf, tor_conf, debug_level_conf) = options.read()
# load config

app_log = log.log("pool.log",debug_level_conf)

def execute(cursor, what):
    # secure execute for slow nodes
    passed = 0
    while passed == 0:
        try:
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

class ThreadedTCPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    pass

class ThreadedTCPRequestHandler(SocketServer.BaseRequestHandler):
    def handle(self):  # server defined here
        global busy
        global banlist
        global warning_list_limit_conf

        peer_ip = self.request.getpeername()[0]

        try:
            data = connections.receive(self.request, 10)

            app_log.info("Incoming: Received: {} from {}".format(data,peer_ip))  # will add custom ports later

            if data == "block":  # from miner

                app_log.warning("Outgoing: Received a block from miner")
                # receive block
                segments = connections.receive(self.request, 10)

                s = socks.socksocket()
                s.connect(("127.0.0.1", 5658))

                connections.send(s, segments, 10)

            else:
                raise ValueError("Unexpected error, received: " + str(data))

            time.sleep(0.1)  # prevent cpu overload
            # app_log.info("Server resting")

        except Exception, e:
            app_log.info("Incoming: Lost connection to {}".format(peer_ip))
            app_log.info("Incoming: {}".format(e))

if __name__ == "__main__":
    try:
        # Port 0 means to select an arbitrary unused port
        HOST, PORT = "0.0.0.0", 3225

        server = ThreadedTCPServer((HOST, PORT), ThreadedTCPRequestHandler)
        ip, port = server.server_address

        # Start a thread with the server -- that thread will then start one
        # more thread for each request

        server_thread = threading.Thread(target=server.serve_forever)

        # Exit the server thread when the main thread terminates

        server_thread.daemon = True
        server_thread.start()
        app_log.warning("Server loop running in thread: {}".format(server_thread.name))

        # server.serve_forever() #added
        server.shutdown()
        server.server_close()

    except Exception, e:
        app_log.info("Pool already running?")
        app_log.info(e)