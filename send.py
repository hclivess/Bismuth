import hashlib
import sqlite3
import socket
import time
import base64
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA

# import keys
key = RSA.importKey(open('privkey.der').read())
public_key = key.publickey()
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
address = hashlib.sha224(public_key_readable).hexdigest()

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#s.settimeout(1)
s.connect(("127.0.0.1", int("2829")))
print "Connected"

#enter transaction start
conn = sqlite3.connect('ledger.db')
c = conn.cursor()
c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
txhash = c.fetchone()[0]
conn.close()
    
to_address = str(raw_input ("Send to address: "))
amount = str(raw_input ("How much to send: "))
timestamp = str(time.time())

transaction = str(timestamp) +":"+ str(address) +":"+ str(to_address) +":"+ str(amount)

#signature = key.sign(transaction, '')
#print "Client: Signature: "+str(signature)
h = SHA.new(transaction)
signer = PKCS1_v1_5.new(key)
signature = signer.sign(h)
signature_enc = base64.b64encode(signature)
print "Client: Encoded Signature: "+str(signature_enc)

verifier = PKCS1_v1_5.new(key)
if verifier.verify(h, signature) == True:
    if int(amount) < 0:
        print "Client: Signature OK, but cannot use negative amounts"

    else:
        conn = sqlite3.connect('ledger.db')
        c = conn.cursor()
        c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
        txhash = str(c.fetchone()[0])
        txhash_new = hashlib.sha224(str(transaction) + str(signature_enc) + str(txhash)).hexdigest() #define new tx hash based on previous #fix asap
        print "Client: New txhash to go with your transaction: "+txhash_new
        conn.close()
           
        print "Client: The signature and control txhash is valid, proceeding to send transaction, signature, new txhash and the public key"
        s.sendall("transaction")
        time.sleep(0.1)
        s.sendall(transaction+";"+str(signature_enc)+";"+public_key_readable+";"+str(txhash_new)) #todo send list
        time.sleep(0.1)
    
else:
    print "Client: Invalid signature"
#enter transaction end

