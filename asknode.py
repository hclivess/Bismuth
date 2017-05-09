import socks, connections

s = socks.socksocket()
s.connect(("127.0.0.1", 5658))

connections.send(s, "getdiff", 10)
diff = connections.receive(s, 10)

print diff

s.close()