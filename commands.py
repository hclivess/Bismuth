import socks, connections, time, sys

print ('Number of arguments:', len(sys.argv), 'arguments.')
print ('Argument List:', str(sys.argv))

try:
    command = sys.argv[1]
except:
    command = "diffget"


s = socks.socksocket()
s.connect(("127.0.0.1", 5658))

def diffget(socket):
    #check difficulty
    connections.send(s, "diffget", 10)
    diff = connections.receive(s, 10)
    print ("Current difficulty: {}".format(diff))
    #check difficulty

def balanceget(socket):
    #get balance
    connections.send(s, "balanceget", 10)
    connections.send(s, "7d5c2999f9a2e44c23e7b2b73b4c0edae308e9d39482bf44da481edc", 10)
    #balance_ledger = connections.receive(s, 10)
    balance_ledger_mempool = connections.receive(s, 10)
    print ("Address balance with mempool: {}".format(balance_ledger_mempool[0]))
    print ("Address credit with mempool: {}".format(balance_ledger_mempool[1]))
    print ("Address debit with mempool: {}".format(balance_ledger_mempool[2]))
    print ("Address fees with mempool: {}".format(balance_ledger_mempool[3]))
    print ("Address rewards with mempool: {}".format(balance_ledger_mempool[4]))
    #print "Address balance without mempool: {}".format(balance_ledger)
    #get balance

#insert to mempool
#DIRECT INSERT, NO REMOTE TX CONSTRUCTION
#connections.send(s, "mpinsert", 10)
#transaction = "('1494941203.13', '4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed', '4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed', '1.00000000', 'AnCAkXrBhqgKItLrbrho3+KNro5GuQNB7zcYlhxMELbiTIOcHZpv/oUazqwDvybp6xKxLWMYt2rmmGPmZ49Q3WG4ikIPkFgYY6XV9Uq+ZsnwjJNTKTwXfj++M/kGle7omUVCsi7PDeijz0HlORRySOM/G0rBnObUahMSvlGnCyo=', 'LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlHZk1BMEdDU3FHU0liM0RRRUJBUVVBQTRHTkFEQ0JpUUtCZ1FES3ZMVGJEeDg1YTF1Z2IvNnhNTWhWT3E2VQoyR2VZVDgrSXEyejlGd0lNUjQwbDJ0dEdxTks3dmFyTmNjRkxJdThLbjRvZ0RRczNXU1dRQ3hOa2haaC9GcXpGCllZYTMvSXRQUGZ6clhxZ2Fqd0Q4cTRadDRZbWp0OCsyQmtJbVBqakZOa3VUUUl6Mkl1M3lGcU9JeExkak13N24KVVZ1OXRGUGlVa0QwVm5EUExRSURBUUFCCi0tLS0tRU5EIFBVQkxJQyBLRVktLS0tLQ==', '0', '')"
#connections.send(s, transaction, 10)
#confirmation = connections.receive(s, 10)
#print (confirmation)
#insert to mempool

def mpget(socket):
    #ask for mempool
    connections.send(s, "mpget", 10)
    mempool = connections.receive(s, 10)
    print ("Current mempool: {}".format(mempool))
    #ask for mempool

def blocklast(socket):
    #get last block
    connections.send(s, "blocklast", 10)
    hash_last = connections.receive(s, 10)

    print ("Last block number: {}".format(hash_last[0]))
    print ("Last block hash: {}".format(hash_last[1]))
    #get last hash


def keygen(socket):
    #generate address
    #RECEIVES PRIVATE KEY FROM NODE
    connections.send(s, "keygen", 10)
    keys_generated = connections.receive(s, 10)

    print ("Private key: {}".format(keys_generated[0]))
    print ("Public key: {}".format(keys_generated[1]))
    print ("Address: {}".format(keys_generated[2]))
    #generate address

def blockget(socket):
    #get block
    connections.send(s, "blockget", 10)
    connections.send(s, "14", 10)
    block_get = connections.receive(s, 10)
    print ("Requested block: {}".format(block_get))
    print ("Requested block number of transactions: {}".format(len(block_get)))
    print ("Requested block height: {}".format(block_get[0][0]))
    #get block

def addlist(socket):
    #get all txs for an address
    connections.send(s, "addlist", 10)
    connections.send(s, "c2221e97878e2f21fb2b5f5ff2b6fb3eb9de14bb53592e58443bde34", 10)
    address_tx_list = connections.receive(s, 10)
    print("All transactions for requested address:")
    for row in address_tx_list:
        print (row)
    #get all txs for an address

def txsend(socket, privkey, recipient):
    #generate transaction
    #SENDS PRIVATE KEY TO NODE
    #uses keys and address of previous "keygen" example
    connections.send(s, "txsend", 10)

    remote_tx_timestamp = '%.2f' % time.time()
    remote_tx_privkey = privkey #node will dump pubkey+address from this
    remote_tx_recipient = recipient #send to self
    remote_tx_amount = "5"
    remote_tx_keep = "0"
    remote_tx_openfield = ""

    connections.send(s, (remote_tx_timestamp, remote_tx_privkey, remote_tx_recipient, remote_tx_amount, remote_tx_keep, remote_tx_openfield), 10)
    #generate transaction



if command == "diffget":
    diffget(s)

elif command == "balanceget":
    balanceget(s)

elif command == "mpget":
    mpget(s)

elif command == "blocklast":
    blocklast(s)

elif command == "keygen":
    keygen(s)

elif command == "blockget":
    blockget(s)

elif command == "addlist":
    addlist(s)

elif command == "txsend":
    txsend(s)

s.close()