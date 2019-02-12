import select, json, platform

# Logical timeout
LTIMEOUT = 45
# Fixed header length
SLEN = 10

def send(sdef, data, slen=SLEN):
    sdef.setblocking(1)
    # Make sure the packet is sent in one call

    sdef.sendall(str(len(str(json.dumps(data)))).encode("utf-8").zfill(slen) + str(json.dumps(data)).encode("utf-8"))

if "Linux" in platform.system():
    READ_OR_ERROR = select.POLLIN | select.POLLPRI | select.POLLHUP | select.POLLERR | select.POLLNVAL
    #READ_ONLY = select.POLLIN | select.POLLPRI

    def receive(sdef, slen=SLEN, timeout=LTIMEOUT):
        try:
            sdef.setblocking(1)
            poller = select.poll()
            poller.register(sdef, READ_OR_ERROR)
            ready = poller.poll(timeout*1000)
            if not ready:
                # logical timeout
                return "*"
            fd, flag = ready[0]
            if (flag & ( select.POLLHUP | select.POLLERR | select.POLLNVAL)):
                # No need to read
                raise RuntimeError("Socket POLLHUP")
            if (flag & (select.POLLIN|select.POLLPRI)):
                data = sdef.recv(slen)
                if not data:
                    # POLLIN and POLLHUP are not exclusive. We can have both.
                    raise RuntimeError("Socket EOF")
                data = int(data)  # receive length
            elif (flag & (select.POLLERR | select.POLLHUP | select.POLLNVAL)):
                raise RuntimeError("Socket error {}".format(flag))
            else:
                raise RuntimeError("Socket Unexpected Error")
            chunks = []
            bytes_recd = 0
            while bytes_recd < data:
                ready = poller.poll(timeout*1000)
                if not ready:
                    raise RuntimeError("Socket Timeout2")
                fd, flag = ready[0]
                if (flag & ( select.POLLHUP | select.POLLERR | select.POLLNVAL)):
                    # No need to read
                    raise RuntimeError("Socket POLLHUP2")
                if (flag & (select.POLLIN|select.POLLPRI)):
                    chunk = sdef.recv(min(data - bytes_recd, 2048))
                    if not chunk:
                        raise RuntimeError("Socket EOF2")
                    chunks.append(chunk)
                    bytes_recd = bytes_recd + len(chunk)
                elif (flag & (select.POLLERR | select.POLLHUP | select.POLLNVAL)):
                    raise RuntimeError("Socket Error {}".format(flag))
                else:
                    raise RuntimeError("Socket Unexpected Error")

            poller.unregister(sdef)
            segments = b''.join(chunks).decode("utf-8")
            return json.loads(segments)
        except Exception as e:
            """
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            """
            # Final cleanup
            try:
                poller.unregister(sdef)
            except Exception as e2:
                pass
                #print ("Exception unregistering: {}".format(e2))
            raise RuntimeError(f"Connections: {e}")


else:

    def receive(sdef, slen=SLEN, timeout=LTIMEOUT):
        sdef.setblocking(1)
        ready = select.select([sdef], [], [sdef], timeout)
        if ready[0]:
            try:
                data = int(sdef.recv(slen))  # receive length
                #print ("To receive: {}".format(data))
            except:
                raise RuntimeError("Connection closed by the remote host")

        else:
            # logical timeoutsha_hash
            return "*"
            #raise RuntimeError("Socket timeout")

        chunks = []
        bytes_recd = 0
        while bytes_recd < data:
            ready = select.select([sdef], [], [], timeout)
            if ready[0]:
                chunk = sdef.recv(min(data - bytes_recd, 2048))
                if not chunk:
                    raise RuntimeError("Socket connection broken")
                chunks.append(chunk)
                bytes_recd = bytes_recd + len(chunk)
            else:
                 raise RuntimeError("Socket timeout")

        segments = b''.join(chunks).decode("utf-8")
        #print(f"Received segments: {segments} from {sdef.getpeername()[0]}")


        return json.loads(segments)
