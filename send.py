import hashlib
import sqlite3
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

#enter transaction start
conn = sqlite3.connect('ledger.db')
c = conn.cursor()
c.execute("SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1;")
block_hash = c.fetchone()[0]
conn.close()
    
to_address = str(raw_input ("Send to address: "))
amount = str(raw_input ("How much to send: "))
openfield = str(raw_input ("Openfield data: "))
timestamp = str(time.time())

transaction = (timestamp,address,to_address,str(float(amount)),openfield)

h = SHA.new(str(transaction))
signer = PKCS1_v1_5.new(key)
signature = signer.sign(h)
signature_enc = base64.b64encode(signature)
print "Client: Encoded Signature: "+str(signature_enc)

verifier = PKCS1_v1_5.new(key)
if verifier.verify(h, signature):
    if int(amount) < 0:
        print "Client: Signature OK, but cannot use negative amounts"

    else:
        print "Client: The signature and control block_hash is valid, proceeding to send transaction, signature, new block_hash and the public key"

        mempool = sqlite3.connect('mempool.db')
        mempool.text_factory = str
        m = mempool.cursor()
        m.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?)", (timestamp,address,to_address,str(float(amount)),signature_enc,public_key_readable,openfield))
        mempool.commit()  # Save (commit) the changes
        mempool.close()
        print "Client: Mempool updated with a received transaction"
    
else:
    print "Client: Invalid signature"
#enter transaction end

