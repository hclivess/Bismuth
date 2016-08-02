import hashlib
import socket
import re
import sqlite3
import os
import sys
import time
import base64

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
from Crypto import Random

if os.path.isfile("privkey.der") is True:
    print "privkey.der found"

else:   
    #generate key pair and an address
    random_generator = Random.new().read
    key = RSA.generate(1024, random_generator)
    public_key = key.publickey()

    private_key_readable = str(key.exportKey())
    public_key_readable = str(key.publickey().exportKey())
    address = hashlib.sha224(public_key_readable).hexdigest() #hashed public key
    #generate key pair and an address

    print "Your address: "+ str(address)
    print "Your private key:\n "+ str(private_key_readable)
    print "Your public key:\n "+ str(public_key_readable)

    pem_file = open("privkey.der", 'a')
    pem_file.write(str(private_key_readable))
    pem_file.close()

    pem_file = open("pubkey.der", 'a')
    pem_file.write(str(public_key_readable))
    pem_file.close()
    
    address_file = open ("address.txt", 'a')
    address_file.write(str(address)+"\n")
    address_file.close()


# import keys
key = RSA.importKey(open('privkey.der').read())
public_key = key.publickey()
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
address = hashlib.sha224(public_key_readable).hexdigest()

print "Your address: "+ str(address)
print "Your private key:\n "+ str(private_key_readable)
print "Your public key:\n "+ str(public_key_readable)
# import keys

timestamp = str(time.time())
print "Timestamp: "+timestamp
transaction = timestamp+":genesis:"+address+":100000000"
h = SHA.new(transaction)
signer = PKCS1_v1_5.new(key)
signature = signer.sign(h)
signature_enc = base64.b64encode(signature)
print "Encoded Signature: "+str(signature_enc)
txhash = hashlib.sha224(str(transaction) + str(signature_enc) + str(public_key_readable)).hexdigest()
print "Transaction Hash:" + txhash

if os.path.isfile("ledger.db") is True:
    print "You are beyond genesis"
else:
    #transaction processing
    con = None
    try:
        conn = sqlite3.connect('ledger.db')
        c = conn.cursor()
        c.execute("CREATE TABLE transactions (block_height INTEGER, timestamp, address, to_address, amount, signature, public_key, txhash, fee, reward, confirmations)")
        c.execute("INSERT INTO transactions VALUES ('1','"+timestamp+"','genesis','"+address+"','100000000','"+str(signature_enc)+"','"+public_key_readable+"','"+txhash+"','0','0','0')") # Insert a row of data
        conn.commit() # Save (commit) the changes

        mempool = sqlite3.connect('mempool.db')
        m = mempool.cursor()
        m.execute("CREATE TABLE transactions (timestamp, address, to_address, amount, signature, public_key)")
        mempool.commit()
        mempool.close()
        
        print "Genesis created, don't forget to hardcode your genesis address"
    except sqlite3.Error, e:                      
        print "Error %s:" % e.args[0]
        sys.exit(1)                        
    finally:                        
        if conn:
            conn.close()
    #transaction processing
