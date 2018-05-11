import socks, connections, time
import json

#define private key
with open ("wallet.der", 'r') as wallet_file:
    wallet_dict = json.load (wallet_file)

private_key_readable = wallet_dict['Private Key']

#define connection and connect
s = socks.socksocket()
s.connect(("127.0.0.1", 5658))


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
    txid = signature[:56]
    print ("Transaction ID",txid)
    return txid

txsend(s, private_key_readable, "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed", "1", "0", "0")
