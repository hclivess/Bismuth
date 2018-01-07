import time
import connections
import socks
from Crypto.PublicKey import RSA

#define private key
with open("privkey.der") as f:
    key = f.read()
priv_key_readable = RSA.importKey(key).exportKey().decode("utf-8") #private key must be decoded

#define connection and connect
s = socks.socksocket()
s.connect(("127.0.0.1", 5658))


def txsend(socket, arg1, arg2, arg3, arg4, arg5):
    #generate transaction
    #SENDS PRIVATE KEY TO NODE
    connections.send(s, "txsend", 10)

    timestamp = '%.2f' % time.time()
    privkey = str(arg)1 #node will dump pubkey+address from this
    recipient = str(arg2)
    amount = str(arg3)
    keep = str(arg4)
    openfield = str(arg5)

    #connections.send(s, (remote_tx_timestamp, remote_tx_privkey, remote_tx_recipient, remote_tx_amount, remote_tx_keep, remote_tx_openfield), 10)
    connections.send(s,
                     (timestamp, privkey, recipient, amount, keep, openfield),
                     10)
    #generate transaction

    signature = connections.receive(s, 10)
    print(signature)

txsend(s, private_key_readable, "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed", "1", "0", "0")
