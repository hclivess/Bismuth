import socks, connections, time, sys, json
import options
config = options.Get()
config.read()
version = config.version_conf

print ('Number of arguments:', len(sys.argv), 'arguments.')
print ('Argument List:', str(sys.argv))

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
    print (entry)

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
        arg5 = entry[5]
    except:
        pass

    try:
        arg6 = entry[6]
    except:
        pass


s = socks.socksocket()
s.settimeout(10)

is_regnet = False

if "testnet" in version:
    s.connect (("127.0.0.1", 2829))
    print("tesnet mode")
elif "regnet" in version:
    is_regnet = True
    print("Regtest mode")
    s.connect (("127.0.0.1", 3030))
else:
    s.connect(("34.192.6.105", 5658))
    #s.connect(("bismuth.live", 5658))

def stop(socket):
    connections.send(s, "stop")


def annverget(socket):
    connections.send(s, "annverget")
    result = connections.receive(s)
    print (result)

def annget(socket):
    connections.send(s, "annget")
    result = connections.receive(s)
    print (result)

def diffget(socket):
    #check difficulty
    connections.send(s, "diffget")
    diff = connections.receive(s)
    print ("Current difficulty: {}".format(diff))
    #check difficulty

def diffgetjson(socket):
    #check difficulty
    connections.send(s, "diffgetjson")
    response = connections.receive(s)
    #for key in response:
    #    print (key,":",response[key])
    print(json.dumps(response))
    #check difficulty

def balanceget(socket, arg1):
    #get balance
    connections.send(s, "balanceget")
    connections.send(s, arg1)
    balanceget_result = connections.receive(s)
    print ("Address balance: {}".format(balanceget_result[0]))
    print ("Address credit: {}".format(balanceget_result[1]))
    print ("Address debit: {}".format(balanceget_result[2]))
    print ("Address fees: {}".format(balanceget_result[3]))
    print ("Address rewards: {}".format(balanceget_result[4]))
    print ("Address balance without mempool: {}".format (balanceget_result[5]))
    #get balance

def balancegetjson(socket, arg1):
    #get balance
    connections.send(s, "balancegetjson")
    connections.send(s, arg1)
    response = connections.receive(s)
    print(json.dumps(response))
    #get balance

def balancegethyper(socket, arg1):
    #get balance
    connections.send(s, "balancegethyper")
    connections.send(s, arg1)
    balanceget_result = connections.receive(s)
    print ("Address balance: {}".format(balanceget_result))
    #get balance

def balancegethyperjson(socket, arg1):
    #get balance
    connections.send(s, "balancegethyperjson")
    connections.send(s, arg1)
    response = connections.receive(s)
    print(json.dumps(response))
    #get balance

#insert to mempool
#DIRECT INSERT, NO REMOTE TX CONSTRUCTION
def mpinsert(s, transaction):
    connections.send(s, "mpinsert")
    connections.send(s, transaction)
    confirmation = connections.receive(s)
    print (confirmation)
#insert to mempool

def mpget(socket):
    #ask for mempool
    connections.send(s, "mpget")
    mempool = connections.receive(s)
    print ("Current mempool: {}".format(mempool))
    #ask for mempool

def mpgetjson(socket):
    #ask for mempool
    connections.send(s, "mpgetjson")
    response_list = connections.receive(s)
    print ("Current mempool:")
    print(json.dumps(response_list))
    #ask for mempool

def difflast(socket):
    #ask for last difficulty
    connections.send(s, "difflast")
    response = connections.receive(s)
    blocklast = response[0]
    difflast = response[1]
    print("Last block: {}".format(blocklast))
    print ("Last difficulty: {}".format(difflast))
    #ask for last difficulty

def difflastjson(socket):
    #ask for last difficulty
    connections.send(s, "difflastjson")
    response = connections.receive(s)
    print(json.dumps(response))
    #ask for last difficulty

def blocklast(socket):
    #get last block
    connections.send(s, "blocklast")
    block_last = connections.receive(s)

    print ("Last block number: {}".format(block_last[0]))
    print ("Last block timestamp: {}".format(block_last[1]))
    print ("Last block hash: {}".format(block_last[7]))
    #get last block

def blocklastjson(socket):
    #get last block
    connections.send(s, "blocklastjson")
    response = connections.receive(s)
    print(json.dumps(response))
    #get last block


def keygen(socket):
    #generate address
    #RECEIVES PRIVATE KEY FROM NODE
    connections.send(s, "keygen")
    keys_generated = connections.receive(s)

    print ("Private key: {}".format(keys_generated[0]))
    print ("Public key: {}".format(keys_generated[1]))
    print ("Address: {}".format(keys_generated[2]))
    #generate address

def keygenjson(socket):
    #generate address
    #RECEIVES PRIVATE KEY FROM NODE
    connections.send(s, "keygenjson")
    response = connections.receive(s)
    print(json.dumps(response))
    #generate address

def blockget(socket, arg1):
    #get block
    connections.send(s, "blockget")
    connections.send(s, arg1)
    block_get = connections.receive(s)
    print ("Requested block: {}".format(block_get))
    print ("Requested block number of transactions: {}".format(len(block_get)))
    print ("Requested block height: {}".format(block_get[0][0]))
    #get block

def blockgetjson(socket, arg1):
    #get block
    connections.send(s, "blockgetjson")
    connections.send(s, arg1)
    response_list = connections.receive(s)
    print(json.dumps(response_list))
    #get block

def addlist(socket, arg1):
    #get all txs for an address
    connections.send(s, "addlist")
    connections.send(s, arg1)
    address_tx_list = connections.receive(s)
    print("All transactions for requested address:")
    for row in address_tx_list:
        print (row)
    #get all txs for an address

def addlistlim(socket, arg1, arg2):
    #get x txs for an address
    connections.send(s, "addlistlim")
    connections.send(s, arg1)
    connections.send(s, arg2)
    address_tx_list = connections.receive(s)
    print("Transactions for requested address:")
    for row in address_tx_list:
        print (row)
    #get all txs for an address

def addlistlimjson(socket, arg1, arg2):
    #get x txs for an address
    connections.send(s, "addlistlimjson")
    connections.send(s, arg1)
    connections.send(s, arg2)
    response_list = connections.receive(s)
    print("Transactions for requested address:")
    print(json.dumps(response_list))
    #get all txs for an address

def addlistlimmir(socket, arg1, arg2):
    #get x negative txs for an address
    connections.send(s, "addlistlimmir")
    connections.send(s, arg1)
    connections.send(s, arg2)
    address_tx_list = connections.receive(s)
    print("Mirror transactions for requested address:")
    for row in address_tx_list:
        print (row)
    #get all txs for an address

def addlistlimmirjson(socket, arg1, arg2):
    #get x negative txs for an address
    connections.send(s, "addlistlimmirjson")
    connections.send(s, arg1)
    connections.send(s, arg2)
    response_list = connections.receive(s)
    print("Mirror transactions for requested address:")
    print(json.dumps(response_list))
    #get all txs for an address

def listlim(socket, arg1):
    #get x last txs
    connections.send(s, "listlim")
    connections.send(s, arg1)
    tx_list = connections.receive(s)
    print("All transactions for requested range:")
    for row in tx_list:
        print (row)

def listlimjson(socket, arg1):
    #get x last txs
    connections.send(s, "listlimjson")
    connections.send(s, arg1)
    response_list = connections.receive(s)
    print("All transactions for requested range:")
    print(json.dumps(response_list))

def txsend(socket, arg1, arg2, arg3, arg4, arg5):
    #generate transaction
    #SENDS PRIVATE KEY TO NODE
    connections.send(s, "txsend")

    remote_tx_timestamp = '%.2f' % time.time()
    remote_tx_privkey = arg1 #node will dump pubkey+address from this
    remote_tx_recipient = arg2
    remote_tx_amount = arg3
    remote_tx_operation = arg4
    remote_tx_openfield = arg5

    #connections.send(s, (remote_tx_timestamp, remote_tx_privkey, remote_tx_recipient, remote_tx_amount, remote_tx_keep, remote_tx_openfield))
    connections.send(s, (str(remote_tx_timestamp), str(remote_tx_privkey), str(remote_tx_recipient), str(remote_tx_amount), str(remote_tx_operation), str(remote_tx_openfield)))
    #generate transaction

    signature = connections.receive(s)
    print (signature)

def aliasget(socket, arg1):
    connections.send(s, "aliasget")
    connections.send(s, arg1)
    alias_results = connections.receive(s)
    print (alias_results)

def tokensget(socket, arg1):
    connections.send(s, "tokensget")
    connections.send(s, arg1)
    tokens_results = connections.receive(s)
    print (tokens_results)

def addfromalias(socket, arg1):
    connections.send(s, "addfromalias")
    connections.send(s, arg1)
    address_fetch = connections.receive(s)
    print (address_fetch)

def peersget(socket):
    connections.send(s, "peersget")
    peers_received = connections.receive(s)
    print (peers_received)

def statusget(socket):
    connections.send(s, "statusjson")
    response = connections.receive(s)
    print(json.dumps(response))

def addvalidate(socket, arg1):
    connections.send(s, "addvalidate")
    connections.send(s, arg1)
    validate_result = connections.receive(s)
    print (validate_result)

def aliasesget(socket, arg1):
    arg_split = arg1.split(",")
    print (arg_split)

    connections.send(s, "aliasesget")
    connections.send(s, arg_split)
    alias_results = connections.receive(s)
    print (alias_results)

def api_getaddresssince(socket, arg1, arg2, arg3):
    connections.send(s, "api_getaddresssince")
    connections.send(s, arg1)
    connections.send(s, arg2)
    connections.send(s, arg3)
    response = connections.receive(s)
    print(json.dumps(response))

if command == "getversion":
    connections.send(s, "getversion")
    print(connections.receive(s))


if command == "generate":
    if not is_regnet:
        print("Only available on regnet")
        sys.exit()
    connections.send(s, "regtest_generate")
    connections.send(s, arg1)
    print(connections.receive(s))

if command == "mpfill":
    if not is_regnet:
        print("Only available on regnet")
        sys.exit()
    connections.send(s, "regtest_mpfill")
    connections.send(s, arg1)
    print(connections.receive(s))


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

elif command == "diffgetjson":
    diffgetjson(s)

elif command == "difflast":
    difflast(s)

elif command == "difflastjson":
    difflastjson(s)

elif command == "balanceget":
    balanceget(s, arg1)

elif command == "balancegetjson":
    balancegetjson(s, arg1)

elif command == "balancegethyper":
    balancegethyper(s, arg1)

elif command == "balancegethyperjson":
    balancegethyperjson(s, arg1)

elif command == "annget":
    annget(s)

elif command == "annverget":
    annverget(s)

elif command == "mpget":
    mpget(s)

elif command == "mpgetjson":
    mpgetjson(s)

elif command == "statusget":
    statusget(s)

elif command == "peersget":
    peersget(s)

elif command == "blocklast":
    blocklast(s)

elif command == "blocklastjson":
    blocklastjson(s)

elif command == "keygen":
    keygen(s)

elif command == "keygenjson":
    keygenjson(s)

elif command == "blockget":
    blockget(s, arg1)

elif command == "blockgetjson":
    blockgetjson(s, arg1)

elif command == "addlist":
    addlist(s, arg1)

elif command == "addlistlim":
    addlistlim(s, arg1, arg2)

elif command == "addlistlimjson":
    addlistlimjson(s, arg1, arg2)

elif command == "addlistlimmir":
    addlistlimmir(s, arg1, arg2)

elif command == "addlistlimmirjson":
    addlistlimmirjson(s, arg1, arg2)

elif command == "listlim":
    listlim(s, arg1)

elif command == "stop":
    stop(s)

elif command == "stop":
    connections.send(s, "stop")

elif command == "addfromalias":
    addfromalias(s, arg1)

elif command == "api_getaddresssince":
    api_getaddresssince(s, arg1, arg2, arg3)

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
