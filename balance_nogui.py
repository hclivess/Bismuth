"""Headless wallet balance query (no GUI).

Standalone CLI helper: loads the local wallet keys, opens a socket to the
running node, issues a ``balanceget`` request for the wallet address and prints
the returned balance / credit / debit / fees / rewards figures (including the
mempool-free balance). Requires a node listening on the configured host/port.
"""

import essentials, options, connections, socks

config = options.Get()
config.read()
node_ip = config.node_ip
port = config.port

key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_b64encoded, address = essentials.keys_load("privkey.der", "pubkey.der")

s = socks.socksocket()
s.settimeout(10)
s.connect((node_ip, int(port)))

connections.send (s, "balanceget", 10)
connections.send (s, address, 10)
balanceget_result = connections.receive (s, 10)
print ("Address balance: {}".format (balanceget_result[0]))
print ("Address credit: {}".format (balanceget_result[1]))
print ("Address debit: {}".format (balanceget_result[2]))
print ("Address fees: {}".format (balanceget_result[3]))
print ("Address rewards: {}".format (balanceget_result[4]))
print ("Address balance without mempool: {}".format (balanceget_result[5]))
# get balance

