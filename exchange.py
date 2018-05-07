import socks, connections, time, sys, json, re
from Crypto.PublicKey import RSA

#define private key
key = RSA.importKey(open('/privkey.der').read())
private_key_readable = str(key.exportKey().decode("utf-8")) #private key must be decoded
exchange_address = "MAIN_EXCHANGE_ADDRESS"

#print ('Number of arguments:', len(sys.argv), 'arguments.')
#print ('Argument List:', str(sys.argv))

def replace_regex_all(string, replace):
    replaced_string = re.sub(r'{}'.format(replace), "", string)
    return replaced_string

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


s = socks.socksocket()
s.connect(("127.0.0.1", 5658))

def getbalance(socket):
    #get balance
    connections.send(s, "balanceget", 10)
    connections.send(s, exchange_address, 10)
    balance_ledger = connections.receive(s, 10)

    result = { 
		u"balance": balance_ledger[0] 
	}
    print (json.dumps(result, indent=2))
    #get balance

def getinfo(socket):
    #get last block
    connections.send(s, "blocklast", 10)
    block_result = connections.receive(s, 10)

    connections.send(s, "statusget", 10)
    status_result = connections.receive(s, 10)

    connections.send(s, "balanceget", 10)
    connections.send(s, exchange_address, 10)
    balance_result  = connections.receive(s, 10)

    connections.send(s, "diffget", 10)
    diff_result = connections.receive(s, 10)

    result = {
        u"version": status_result[6],
        u"protocolversion": 0,
        u"walletversion": 0,
        u"balance": balance_result[0],
        u"blocks": block_result[0],
        u"timeoffset": 0,
        u"connections": status_result[0],
        u"difficulty": diff_result[0],
        u"errors": "",
        u"consensus": status_result[5]
    }
    print (json.dumps(result, indent=2))
    #get last hash

def gettransactions(socket):
    #get all txs for an address
    connections.send(s, "addlist", 10)
    connections.send(s, exchange_address, 10)
    address_tx_list = connections.receive(s, 10)

    connections.send(s, "blocklast", 10)
    block_result = connections.receive(s, 10)

    connections.send(s, "statusget", 10)
    status_result = connections.receive(s, 10)

    result = []

    if status_result[5] > 10:
        for row in address_tx_list:
            result.append({
				u"address": row[11],
				u"category": (row[3] == exchange_address) and "receive" or "send",
				u"amount": row[4],
				u"confirmations": (block_result[0] - row[0]),
				u"blockhash": row[0],
				u"blockindex": row[0],
				u"blocktime": int(float(row[1])),
				u"txid": row[5][:56],
				u"time": int(float(row[1])),
				u"timereceived": int(float(row[1]))
        })

    print(json.dumps(result, indent=2))
    #get all txs for an address

def sendtransaction(socket, arg1, arg2, arg3):
    #generate transaction
    connections.send(s, "txsend", 10)

    remote_tx_timestamp = '%.2f' % time.time()
    remote_tx_privkey = private_key_readable 
    remote_tx_recipient = arg1
    remote_tx_amount = arg2
    remote_tx_operation = '0'
    remote_tx_openfield = arg3

    connections.send(s, (str(remote_tx_timestamp), str(remote_tx_privkey), str(remote_tx_recipient), str(remote_tx_amount), str(remote_tx_operation), str(remote_tx_openfield)), 10)
    tx_id = connections.receive(s, 10)

    result = {
		u"txid": tx_id[:56]
	}
    print(json.dumps(result, indent=2))
    #generate transaction

def validateaddress(socket, arg1):
    #validateAddress
    connections.send(s, "addvalidate", 10)
    connections.send(s, arg1, 10)
    validate_result = connections.receive(s, 10)
    if validate_result == "invalid":
       print (json.dumps({ u"isvalid": False }, indent=2))
    else:
       print (json.dumps({ u"isvalid": True }, indent=2))

    #validateAddress

if command == "getbalance":
    getbalance(s)

elif command == "getinfo":
    getinfo(s)

elif command == "gettransactions":
    gettransactions(s)

elif command == "sendtransaction":
    try:
        arg3 = replace_regex_all(arg3, "token:issue:")
        arg3 = replace_regex_all(arg3, "alias=")
        arg3.encode().decode()

    except:
        arg3=""

    sendtransaction(s, arg1, arg2, arg3)

elif command == "validateaddress":
    validateaddress(s, arg1)

s.close()
