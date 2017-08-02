import select, picklemagic, ast

def send(sdef, data, slen):
    sdef.setblocking(0)

    sdef.sendall(str(len(str(picklemagic.safe_dumps(data)))).encode("utf-8").zfill(slen))
    sdef.sendall(str(picklemagic.safe_dumps(data)).encode("utf-8"))
    #print(pickle.dumps(data))

def receive(sdef, slen):
    sdef.setblocking(0)
    ready = select.select([sdef], [], [], 60)
    if ready[0]:
        data = int(sdef.recv(slen))  # receive length
        #print ("To receive: {}".format(data))
    else:
        raise RuntimeError("Socket timeout")

    chunks = []
    bytes_recd = 0
    while bytes_recd < data:
        ready = select.select([sdef], [], [], 60)
        if ready[0]:
            chunk = sdef.recv(min(data - bytes_recd, 2048))
            if chunk == b'':
                raise RuntimeError("Socket connection broken")
            chunks.append(chunk)
            bytes_recd = bytes_recd + len(chunk)
        else:
             raise RuntimeError("Socket timeout")

    segments = b''.join(chunks).decode("utf-8")
    #print ("Received segments: {}".format(segments))

    return picklemagic.safe_loads(ast.literal_eval(segments))
