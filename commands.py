import socks, connections, time, sys

#print ('Number of arguments:', len(sys.argv), 'arguments.')
#print ('Argument List:', str(sys.argv))

try:
    command = sys.argv[1]
except:
    pass

try:
    arg1 = sys.argv[2]
except:
    pass

try:
    arg2 = sys.argv[3]
except:
    pass

try:
    arg3 = sys.argv[4]
except:
    pass

try:
    arg4 = sys.argv[5]
except:
    pass

try:
    arg5 = sys.argv[6]
except:
    pass

s = socks.socksocket()
s.connect(("127.0.0.1", 5658))

def diffget(socket):
    #check difficulty
    connections.send(s, "diffget", 10)
    diff = connections.receive(s, 10)
    print ("Current difficulty: {}".format(diff))
    #check difficulty

def balanceget(socket, arg1):
    #get balance
    connections.send(s, "balanceget", 10)
    connections.send(s, arg1, 10)
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

def blockget(socket, arg1):
    #get block
    connections.send(s, "blockget", 10)
    connections.send(s, arg1, 10)
    block_get = connections.receive(s, 10)
    print ("Requested block: {}".format(block_get))
    print ("Requested block number of transactions: {}".format(len(block_get)))
    print ("Requested block height: {}".format(block_get[0][0]))
    #get block

def addlist(socket, arg1):
    #get all txs for an address
    connections.send(s, "addlist", 10)
    connections.send(s, arg1, 10)
    address_tx_list = connections.receive(s, 10)
    print("All transactions for requested address:")
    for row in address_tx_list:
        print (row)
    #get all txs for an address

def addlistlim(socket, arg1, arg2):
    #get all txs for an address
    connections.send(s, "addlistlim", 10)
    connections.send(s, arg1, 10)
    connections.send(s, arg2, 10)
    address_tx_list = connections.receive(s, 10)
    print("All transactions for requested address:")
    for row in address_tx_list:
        print (row)
    #get all txs for an address

def txsend(socket, arg1, arg2, arg3, arg4, arg5):
    #generate transaction
    #SENDS PRIVATE KEY TO NODE
    connections.send(s, "txsend", 10)

    remote_tx_timestamp = '%.2f' % time.time()
    remote_tx_privkey = arg1 #node will dump pubkey+address from this
    remote_tx_recipient = arg2
    remote_tx_amount = arg3
    remote_tx_keep = arg4
    remote_tx_openfield = arg5

    #connections.send(s, (remote_tx_timestamp, remote_tx_privkey, remote_tx_recipient, remote_tx_amount, remote_tx_keep, remote_tx_openfield), 10)
    connections.send(s, (str(remote_tx_timestamp), str(remote_tx_privkey), str(remote_tx_recipient), str(remote_tx_amount), str(remote_tx_keep), str(remote_tx_openfield)), 10)
    #generate transaction

def aliasget(socket, arg1):
    connections.send(s, "aliasget", 10)
    connections.send(s, arg1, 10)
    alias_results = connections.receive(s, 10)
    print (alias_results)

def aliasesget(socket, arg1):
    arg_split = arg1.split(",")
    print (arg_split)

    connections.send(s, "aliasesget", 10)
    connections.send(s, arg_split, 10)
    alias_results = connections.receive(s, 10)
    print (alias_results)

if command == "aliasget":
    aliasget(s, arg1)

if command == "aliasesget":
    aliasesget(s, arg1)

elif command == "diffget":
    diffget(s)

elif command == "balanceget":
    balanceget(s, arg1)

elif command == "mpget":
    mpget(s)

elif command == "blocklast":
    blocklast(s)

elif command == "keygen":
    keygen(s)

elif command == "blockget":
    blockget(s, arg1)

elif command == "addlist":
    addlist(s, arg1)

elif command == "txsend":
    try:
        arg4
    except:
        arg4="0"

    try:
        arg5
    except:
        arg5=""

    txsend(s, arg1, arg2, arg3, arg4, arg5)

s.close()