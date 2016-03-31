import socket
import threading
import SocketServer

class ThreadedTCPRequestHandler(SocketServer.BaseRequestHandler):

    def handle(self):

                data = self.request.recv(1024)
                cur_thread = threading.current_thread()
                response = data
                self.request.sendall(response)


class ThreadedTCPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    pass

def client(ip, port, message):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((ip, port))
    try:
        sock.sendall(message)
        response = sock.recv(5)
        print "Received: {}".format(response)
    finally:
        sock.close()

if __name__ == "__main__":
    while True:
        try:
            # Port 0 means to select an arbitrary unused port
            HOST, PORT = "localhost", 0

            server = ThreadedTCPServer((HOST, PORT), ThreadedTCPRequestHandler)
            ip, port = server.server_address

            # Start a thread with the server -- that thread will then start one
            # more thread for each request
            server_thread = threading.Thread(target=server.serve_forever)
            # Exit the server thread when the main thread terminates
            server_thread.daemon = True
            server_thread.start()
            print "Server loop running in thread:", server_thread.name

            client(ip, port, "Hello World 1")
            client(ip, port, "Hello World 2")
            client(ip, port, "Hello World 3")

            server.shutdown()
            server.server_close()

        except:
            raise            


