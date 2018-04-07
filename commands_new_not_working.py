import socks, connections, time, sys
import argparse
from functools import partial
import types
import atexit

#print('Number of arguments:', len(sys.argv), 'arguments.')
#print('Argument List:', str(sys.argv))
try:
    parse = argparse.ArgumentParser()
    parse.add_argument('command', help="run the command.py of command")
    parse.add_argument('-a', '--arg', action='append', dest='args', help="command's args")
    args = parse.parse_args()
    command = args.command
    args = args.args if args.args else []

    """
    SAMPLE:
        python commands.py diffget
        python commands.py txsend -a 1 -a 2 -a 3 -a 4 -a 5
    """

    args.extend([''] * (5 - len(args)))

    arg1, arg2, arg3, arg4, arg5 = args
    arg4 = arg4 or 0

except:
    entry = input("No argument detected, please insert command manually\n").split()

    entry.extend([''] * (6 - len(entry)))
    command, arg1, arg2, arg3, arg4, arg5 = entry

s = socks.socksocket()
s.settimeout(10)
s.connect(("127.0.0.1", 5658))
atexit.register(s.close)
#s.connect(("94.113.207.67", 5658))

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
    balance_ledger = connections.receive(s, 10)
    print("Address balance: {}".format(balance_ledger[0]))
    print("Address credit: {}".format(balance_ledger[1]))
    print("Address debit: {}".format(balance_ledger[2]))
    print("Address fees: {}".format(balance_ledger[3]))
    print("Address rewards: {}".format(balance_ledger[4]))
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
    print("Current mempool: {}".format(mempool))
    #ask for mempool

def difflast(socket):
    #ask for last difficulty
    connections.send(s, "difflast", 10)
    response = connections.receive(s, 10)
    blocklast = response[0]
    difflast = response[1]
    print("Last block: {}".format(blocklast))
    print("Last difficulty: {}".format(difflast))
    #ask for last difficulty

def blocklast(socket):
    #get last block
    connections.send(s, "blocklast", 10)
    block_last = connections.receive(s, 10)

    print("Last block number: {}".format(block_last[0]))
    print("Last block timestamp: {}".format(block_last[1]))
    #get last block


def keygen(socket):
    #generate address
    #RECEIVES PRIVATE KEY FROM NODE
    connections.send(s, "keygen", 10)
    keys_generated = connections.receive(s, 10)

    print("Private key: {}".format(keys_generated[0]))
    print("Public key: {}".format(keys_generated[1]))
    print("Address: {}".format(keys_generated[2]))
    #generate address

def blockget(socket, arg1):
    #get block
    connections.send(s, "blockget", 10)
    connections.send(s, arg1, 10)
    block_get = connections.receive(s, 10)
    print("Requested block: {}".format(block_get))
    print("Requested block number of transactions: {}".format(len(block_get)))
    print("Requested block height: {}".format(block_get[0][0]))
    #get block

def addlist(socket, arg1):
    #get all txs for an address
    connections.send(s, "addlist", 10)
    connections.send(s, arg1, 10)
    address_tx_list = connections.receive(s, 10)
    print("All transactions for requested address:")
    for row in address_tx_list:
        print(row)
    #get all txs for an address

def addlistlim(socket, arg1, arg2):
    #get all txs for an address
    connections.send(s, "addlistlim", 10)
    connections.send(s, arg1, 10)
    connections.send(s, arg2, 10)
    address_tx_list = connections.receive(s, 10)
    print("Transactions for requested address:")
    for row in address_tx_list:
        print(row)
    #get all txs for an address

def listlim(socket, arg1):
    #get all txs for an address
    connections.send(s, "listlim", 10)
    connections.send(s, arg1, 10)
    tx_list = connections.receive(s, 10)
    print("All transactions for requested range:")
    for row in tx_list:
        print(row)

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
    print(signature)

def aliasget(socket, arg1):
    connections.send(s, "aliasget", 10)
    connections.send(s, arg1, 10)
    alias_results = connections.receive(s, 10)
    print(alias_results)

def peersget(socket):
    connections.send(s, "peersget", 10)
    peers_received = connections.receive(s, 10)
    print(peers_received)

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
    print(validate_result)

def aliasesget(socket, arg1):
    arg_split = arg1.split(",")
    print(arg_split)

    connections.send(s, "aliasesget", 10)
    connections.send(s, arg_split, 10)
    alias_results = connections.receive(s, 10)
    print(alias_results)

# init command and args, According to the number of different parameters
cmd_dict = {cmd: [] for cmd in ['diffget', 'difflast', 'mpget', 'statusget', 'peersget', 'blocklast', 'keygen', ]}
cmd_dict.update({
    cmd: [arg1, ] for cmd in ['aliasget', 'addvalidate', 'aliasesget', 'balanceget', "blockget", "addlist", "listlim", ]

})
cmd_dict.update({cmd: [arg1, arg2] for cmd in ["addlistlim", ]})
cmd_dict.update({cmd: [arg1, arg2, arg3, arg4, arg5]for cmd in ["txsend", ]})

if command in cmd_dict.keys():
    cmd = globals().get(command)
    assert isinstance(cmd, types.FunctionType)
    cmd = partial(cmd, socket=s)
    cmd(*cmd_dict[command])
else:
    print("Command not known")
