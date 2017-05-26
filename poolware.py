import SocketServer, connections, time, options, log, methods
(port, genesis_conf, verify_conf, version_conf, thread_limit_conf, rebuild_db_conf, debug_conf, purge_conf, pause_conf, ledger_path_conf, hyperblocks_conf, warning_list_limit_conf, tor_conf, debug_level_conf, allowed) = options.read()
app_log = log.log("pool.log",debug_level_conf)

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
            diff = methods.diffget()
            connections.send(self.request, diff, 10)

if __name__ == "__main__":
    HOST, PORT = "localhost", 8525

    # Create the server, binding to localhost on port 9999
    server = SocketServer.TCPServer((HOST, PORT), MyTCPHandler)

    # Activate the server; this will keep running until you
    # interrupt the program with Ctrl-C
    server.serve_forever()