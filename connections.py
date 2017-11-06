import select, json, os, sys

def send(sdef, data, slen):
    sdef.setblocking(0)
    sdef.sendall(str(len(str(json.dumps(data)))).encode("utf-8").zfill(slen))
    sdef.sendall(str(json.dumps(data)).encode("utf-8"))

if "posix" not in os.name:

    def receive(sdef, slen):
        sdef.setblocking(0)
        ready = select.select([sdef], [], [], 30)
        if ready[0]:
            try:
                data = int(sdef.recv(slen))  # receive length
            except:
                raise RuntimeError("Connection closed by the remote host") #do away with the invalid literal for int
        else:
            raise RuntimeError("Socket timeout")

        chunks = []
        bytes_recd = 0
        while bytes_recd < data:
            ready = select.select([sdef], [], [], 30)
            if ready[0]:
                chunk = sdef.recv(min(data - bytes_recd, 2048))
                if not chunk:
                    raise RuntimeError("Socket connection broken")
                chunks.append(chunk)
                bytes_recd = bytes_recd + len(chunk)
            else:
                 raise RuntimeError("Socket timeout")

        segments = b''.join(chunks).decode("utf-8")
        #print("Received segments: {}".format(segments))
        return json.loads(segments)

else:
    READ_OR_ERROR = select.POLLIN | select.POLLPRI | select.POLLHUP | select.POLLERR | select.POLLNVAL
    #READ_ONLY = select.POLLIN | select.POLLPRI 

    def receive(sdef, slen):
        try:
            sdef.setblocking(0) 
            poller = select.epoll()
            poller.register(sdef, READ_OR_ERROR)
            ready = poller.poll(1000)
            fd, flag = ready[0]
            if (flag & (select.POLLIN|select.POLLPRI)):
                data = sdef.recv(slen)
                if not data:
                    peer_ip = sdef.getpeername()[0]
                    raise RuntimeError("Socket error0 {} for {}".format(flag,peer_ip))
                data = int(data)  # receive length
            elif (flag & (select.POLLERR | select.POLLHUP | select.POLLNVAL)):     
                raise RuntimeError("Socket error1 {}".format(flag))
            else:
                raise RuntimeError("Socket timeout1")
            chunks = []
            bytes_recd = 0
            while bytes_recd < data:
                ready = poller.poll(1000)
                fd, flag = ready[0]
                if (flag & (select.POLLIN|select.POLLPRI)):
                    chunk = sdef.recv(min(data - bytes_recd, 2048))
                    if not chunk:
                        raise RuntimeError("Socket connection broken0")
                    chunks.append(chunk)
                    bytes_recd = bytes_recd + len(chunk)
                elif (flag & (select.POLLERR | select.POLLHUP | select.POLLNVAL)):       
                    raise RuntimeError("Socket error2 {}".format(flag))
                else:
                    raise RuntimeError("Socket timeout2")

            poller.unregister(sdef)
            segments = b''.join(chunks).decode("utf-8")
            return json.loads(segments)
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            # uncomment the following to check for detailled errors within connections.py
            #print(exc_type, fname, exc_tb.tb_lineno)
            # Cleanup
            try:
                poller.unregister(sdef)
            except Exception as e2:
                pass
            raise RuntimeError("Exception in Receive : {}".format(e))
