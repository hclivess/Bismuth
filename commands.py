import socks, connections, time, sys
import options
config = options.Get()
config.read()
version = config.version_conf

#print ('Number of arguments:', len(sys.argv), 'arguments.')
#print ('Argument List:', str(sys.argv))

try:
    command = sys.argv[1]

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

except:

    entry = input("No argument detected, please insert command manually\n").split()
    command = entry[0]
    try:
        arg1 = entry[1]
    except:
        pass
    try:
        arg2 = entry[2]
    except:
        pass
    try:
        arg3 = entry[3]
    except:
        pass
    try:
        arg4 = entry[4]
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
s.settimeout(10)



if "testnet" in version:
    s.connect (("127.0.0.1", 2829))
    print("tesnet mode")
else:
    s.connect(("127.0.0.1", 5658))
#s.connect(("94.113.207.67", 5658))

def shutdown(socket):
    connections.send(s, "shutdown", 10)

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
    balanceget_result = connections.receive(s, 10)
    print ("Address balance: {}".format(balanceget_result[0]))
    print ("Address credit: {}".format(balanceget_result[1]))
    print ("Address debit: {}".format(balanceget_result[2]))
    print ("Address fees: {}".format(balanceget_result[3]))
    print ("Address rewards: {}".format(balanceget_result[4]))
    print ("Address balance without mempool: {}".format (balanceget_result[5]))
    #get balance

#insert to mempool
#DIRECT INSERT, NO REMOTE TX CONSTRUCTION
def mpinsert(s, transaction):
    connections.send(s, "mpinsert", 10)
    connections.send(s, transaction, 10)
    confirmation = connections.receive(s, 10)
    print (confirmation)
#insert to mempool

def mpget(socket):
    #ask for mempool
    connections.send(s, "mpget", 10)
    mempool = connections.receive(s, 10)
    print ("Current mempool: {}".format(mempool))
    #ask for mempool

def difflast(socket):
    #ask for last difficulty
    connections.send(s, "difflast", 10)
    response = connections.receive(s, 10)
    blocklast = response[0]
    difflast = response[1]
    print("Last block: {}".format(blocklast))
    print ("Last difficulty: {}".format(difflast))
    #ask for last difficulty

def blocklast(socket):
    #get last block
    connections.send(s, "blocklast", 10)
    block_last = connections.receive(s, 10)

    print ("Last block number: {}".format(block_last[0]))
    print ("Last block timestamp: {}".format(block_last[1]))
    print ("Last block hash: {}".format(block_last[7]))
    #get last block


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
    print("Transactions for requested address:")
    for row in address_tx_list:
        print (row)
    #get all txs for an address

def listlim(socket, arg1):
    #get all txs for an address
    connections.send(s, "listlim", 10)
    connections.send(s, arg1, 10)
    tx_list = connections.receive(s, 10)
    print("All transactions for requested range:")
    for row in tx_list:
        print (row)

def txsend(socket, arg1, arg2, arg3, arg4, arg5):
    #generate transaction
    #SENDS PRIVATE KEY TO NODE
    connections.send(s, "txsend", 10)

    remote_tx_timestamp = '%.2f' % time.time()
    remote_tx_privkey = arg1 #node will dump pubkey+address from this
    remote_tx_recipient = arg2
    remote_tx_amount = arg3
    remote_tx_operation = arg4
    remote_tx_openfield = arg5

    #connections.send(s, (remote_tx_timestamp, remote_tx_privkey, remote_tx_recipient, remote_tx_amount, remote_tx_keep, remote_tx_openfield), 10)
    connections.send(s, (str(remote_tx_timestamp), str(remote_tx_privkey), str(remote_tx_recipient), str(remote_tx_amount), str(remote_tx_operation), str(remote_tx_openfield)), 10)
    #generate transaction

    signature = connections.receive(s, 10)
    print (signature)

def aliasget(socket, arg1):
    connections.send(s, "aliasget", 10)
    connections.send(s, arg1, 10)
    alias_results = connections.receive(s, 10)
    print (alias_results)

def tokensget(socket, arg1):
    connections.send(s, "tokensget", 10)
    connections.send(s, arg1, 10)
    tokens_results = connections.receive(s, 10)
    print (tokens_results)

def addfromalias(socket, arg1):
    connections.send(s, "addfromalias", 10)
    connections.send(s, arg1, 10)
    address_fetch = connections.receive(s, 10)
    print (address_fetch)

def peersget(socket):
    connections.send(s, "peersget", 10)
    peers_received = connections.receive(s, 10)
    print (peers_received)

def statusget(socket):
    connections.send(s, "statusget", 10)
    response = connections.receive(s, 10)
    node_address = response[0]
    nodes_count = response[1]
    nodes_list = response[2]
    threads_count = response[3]
    uptime = response[4]
    consensus = response[5]
    consensus_percentage = response[6]
    version = response[7]
    print("Node address:", node_address)
    print("Number of nodes:", nodes_count)
    print("List of nodes:", nodes_list)
    print("Number of threads:", threads_count)
    print("Uptime:", uptime)
    print("Consensus:", consensus)
    print("Consensus percentage:", consensus_percentage)
    print("Version:", version)

def addvalidate(socket, arg1):
    connections.send(s, "addvalidate", 10)
    connections.send(s, arg1, 10)
    validate_result = connections.receive(s, 10)
    print (validate_result)

def aliasesget(socket, arg1):
    arg_split = arg1.split(",")
    print (arg_split)

    connections.send(s, "aliasesget", 10)
    connections.send(s, arg_split, 10)
    alias_results = connections.receive(s, 10)
    print (alias_results)

if command == "mpinsert":
    #arg1 = '1520788207.69', '4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed', '4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed', '0.00000000', 'e0piKXvc636t0fYmxdOti3fJZ+G1vQYAJ2IZv4inPGQYgG4nS0lU+61LDQQVqeGvmsDOsxFhM6VVLpYExPmc5HF6e1ZAr5IXQ69s88sJBx/XVl1YavAdo0katGDyvZpQf609F8PVbtD0zzBinQjfkoXU/NXo00CEyniyYPxAXuI=', 'LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlHZk1BMEdDU3FHU0liM0RRRUJBUVVBQTRHTkFEQ0JpUUtCZ1FES3ZMVGJEeDg1YTF1Z2IvNnhNTWhWT3E2VQoyR2VZVDgrSXEyejlGd0lNUjQwbDJ0dEdxTks3dmFyTmNjRkxJdThLbjRvZ0RRczNXU1dRQ3hOa2haaC9GcXpGCllZYTMvSXRQUGZ6clhxZ2Fqd0Q4cTRadDRZbWp0OCsyQmtJbVBqakZOa3VUUUl6Mkl1M3lGcU9JeExkak13N24KVVZ1OXRGUGlVa0QwVm5EUExRSURBUUFCCi0tLS0tRU5EIFBVQkxJQyBLRVktLS0tLQ==', '0', ''
    mpinsert(s, arg1)

if command == "aliasget":
    aliasget(s, arg1)

if command == "tokensget":
    tokensget(s, arg1)

if command == "addvalidate":
    addvalidate(s, arg1)

if command == "aliasesget":
    aliasesget(s, arg1)

elif command == "diffget":
    diffget(s)

elif command == "difflast":
    difflast(s)

elif command == "balanceget":
    balanceget(s, arg1)

elif command == "mpget":
    mpget(s)

elif command == "statusget":
    statusget(s)

elif command == "peersget":
    peersget(s)

elif command == "blocklast":
    blocklast(s)

elif command == "keygen":
    keygen(s)

elif command == "blockget":
    blockget(s, arg1)

elif command == "addlist":
    addlist(s, arg1)

elif command == "addlistlim":
    addlistlim(s, arg1, arg2)

elif command == "listlim":
    listlim(s, arg1)

elif command == "shutdown":
    shutdown(s)

elif command == "addfromalias":
    addfromalias(s, arg1)

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