import select, json

def send(sdef, data, slen):
    sdef.setblocking(0)

    sdef.sendall(str(len(str(json.dumps(data)))).encode("utf-8").zfill(slen))
    sdef.sendall(str(json.dumps(data)).encode("utf-8"))

def receive(sdef, slen):
    sdef.setblocking(0)
    ready = select.select([sdef], [], [], 5)
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
        ready = select.select([sdef], [], [], 5)
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


