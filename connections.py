import select

def send(sdef, data):
    sdef.setblocking(0)
    sdef.sendall(data)


def receive(sdef, slen):
    sdef.setblocking(0)  # needs adjustments in core mechanics
    ready = select.select([sdef], [], [], 240)
    if ready[0]:
        data = int(sdef.recv(slen))  # receive length
        # print "To receive: "+str(data)
    else:
        raise RuntimeError("Socket timeout")

    chunks = []
    bytes_recd = 0
    while bytes_recd < data:
        ready = select.select([sdef], [], [], 480)
        if ready[0]:
            chunk = sdef.recv(min(data - bytes_recd, 2048))
            if chunk == b'':
                raise RuntimeError("Socket connection broken")
            chunks.append(chunk)
            bytes_recd = bytes_recd + len(chunk)
        else:
            raise RuntimeError("Socket timeout")

    segments = b''.join(chunks)
    # print "Received segments: "+str(segments)

    return segments