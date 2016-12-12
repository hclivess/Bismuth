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

player = [5,6,7,8,9]
bank = [0,1,2,3,4]

conn = sqlite3.connect('ledger.db')
conn.text_factory = str
c = conn.cursor()
c.execute("select * from transactions where recipient = '"+address+"'")
result_bets = c.fetchall()

c.execute("select * from transactions where address = '"+address+"'")
result_payouts = c.fetchall()

won_count = 0
lost_count = 0
txs_winning = []
payout_missing = []

for x in result_bets:
    openfield = x[11]
    block_hash = x[7]
    #print block_hash
    tx_signature = x[5] #unique
    #print tx_signature
    digit_last = (re.findall("(\d)", block_hash))[-1]
    #print digit_last
    if int(digit_last) in player:
        #print "player wins"
        won_count = won_count + 1

        if openfield == base64.b64encode("payout for "+tx_signature):
            print "paid already"
        else:
            payout_missing.append(x)
    else:
        #print "bank wins"
        lost_count = lost_count + 1

print lost_count
print won_count
conn.close()

