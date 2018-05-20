import essentials, options, connections, socks

config = options.Get()
config.read()

key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_hashed, address = essentials.keys_load("privkey.der", "pubkey.der")

s = socks.socksocket()
s.settimeout(10)
s.connect(("127.0.0.1", 5658))

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

