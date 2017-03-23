import socket
import pyping

r = pyping.ping('google.com')                # Need to be root or
r = pyping.ping('google.com', udp = False)    # But it's udp, not real icmp
r.ret_code

print(socket.gethostname())
print r.destination
print r.max_rtt
print r.avg_rtt
print r.min_rtt
print r.destination_ip
