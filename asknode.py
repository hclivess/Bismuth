import socks, connections, ast

s = socks.socksocket()
s.connect(("127.0.0.1", 5658))

#check difficulty
connections.send(s, "diffget", 10)
diff = connections.receive(s, 10)
print "Current difficulty: {}".format(diff)
#check difficulty

#get balance
connections.send(s, "balanceget", 10)
connections.send(s, "f1e5133ff3685f70b9291922dd99a891d1ff4d6226fc6404a16729bf", 10)
balance_ledger = connections.receive(s, 10)
balance_ledger_mempool = connections.receive(s, 10)
print "Address balance with mempool: {}".format(balance_ledger_mempool)
print "Address balance without mempool: {}".format(balance_ledger)
#get balance

#insert to mempool
connections.send(s, "mpinsert", 10)
transaction = "('1494941203.13', '4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed', '4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed', '1.00000000', 'AnCAkXrBhqgKItLrbrho3+KNro5GuQNB7zcYlhxMELbiTIOcHZpv/oUazqwDvybp6xKxLWMYt2rmmGPmZ49Q3WG4ikIPkFgYY6XV9Uq+ZsnwjJNTKTwXfj++M/kGle7omUVCsi7PDeijz0HlORRySOM/G0rBnObUahMSvlGnCyo=', 'LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlHZk1BMEdDU3FHU0liM0RRRUJBUVVBQTRHTkFEQ0JpUUtCZ1FES3ZMVGJEeDg1YTF1Z2IvNnhNTWhWT3E2VQoyR2VZVDgrSXEyejlGd0lNUjQwbDJ0dEdxTks3dmFyTmNjRkxJdThLbjRvZ0RRczNXU1dRQ3hOa2haaC9GcXpGCllZYTMvSXRQUGZ6clhxZ2Fqd0Q4cTRadDRZbWp0OCsyQmtJbVBqakZOa3VUUUl6Mkl1M3lGcU9JeExkak13N24KVVZ1OXRGUGlVa0QwVm5EUExRSURBUUFCCi0tLS0tRU5EIFBVQkxJQyBLRVktLS0tLQ==', '0', '')"
connections.send(s, transaction, 10)
confirmation = connections.receive(s, 10)
print confirmation
#insert to mempool

#ask for mempool
connections.send(s, "mpget", 10)
mempool = connections.receive(s, 10)
print "Current mempool: {}".format(mempool)
#ask for mempool

#get last hash
connections.send(s, "hashlast", 10)
hash_last = connections.receive(s, 10)
print "Last block hash: {}".format(hash_last)
#get last hash

#get block
connections.send(s, "blockget", 10)
connections.send(s, "14", 10)
block_get = ast.literal_eval(connections.receive(s, 10))
print "Requested block: {}".format(block_get)
print "Requested block number of transactions: {}".format(len(block_get))
print "Requested block height: {}".format(block_get[0][0])
#get block

s.close()