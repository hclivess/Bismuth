import sqlite3
import hashlib
import base64
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
import re


key = RSA.importKey(open('privkey.der').read())
public_key = key.publickey()
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
public_key_hashed = base64.b64encode(public_key_readable)
address = hashlib.sha224(public_key_readable).hexdigest()

winner = [5,6,7,8,9]
loser = [0,1,2,3,4]

conn = sqlite3.connect('ledger.db')
conn.text_factory = str
c = conn.cursor()
c.execute("select * from transactions where recipient = '"+address+"'")
result = c.fetchall()

for x in result:
    print x[7]
    digit_last = (re.findall("(\d)", x[7]))[-1]
    print digit_last
    if int(digit_last) in winner:
        print "winner"


conn.close()

