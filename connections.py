import select, json, os


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
                #print ("To receive: {}".format(data))
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
    def receive(sdef, slen):
        sdef.setblocking(0)  # needs adjustments in core mechanics

        READ_ONLY = select.POLLIN | select.POLLPRI | select.POLLHUP | select.POLLERR
        READ_WRITE = READ_ONLY | select.POLLOUT
        poller = select.epoll()
        poller.register(sdef, READ_ONLY)

        ready = poller.poll(240)
        # print(ready)
        if ready[0]:
            data = int(sdef.recv(slen))  # receive length
            # print "To receive: "+str(data)
        else:
            raise RuntimeError("Socket timeout")
        chunks = []
        bytes_recd = 0
        while bytes_recd < data:
            ready = poller.poll(480)
            if ready[0]:
                chunk = sdef.recv(min(data - bytes_recd, 2048))
                if not chunk:
                    raise RuntimeError("Socket connection broken")
                chunks.append(chunk)
                bytes_recd = bytes_recd + len(chunk)
            else:
                raise RuntimeError("Socket timeout")

        poller.unregister(sdef)
        # print ("Received segments: {}".format(segments))

        segments = b''.join(chunks).decode("utf-8")
        return json.loads(segments)
