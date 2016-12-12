import sqlite3
import hashlib
import base64
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
import re
import time

key = RSA.importKey(open('privkey.der').read())
public_key = key.publickey()
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
public_key_hashed = base64.b64encode(public_key_readable)
address = hashlib.sha224(public_key_readable).hexdigest()

player = [5,6,7,8,9]
bank = [0,1,2,3,4]
bet_max = 1000

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
    bet_amount = float(x[4])
    openfield = x[11]
    block_hash = x[7]
    #print block_hash
    tx_signature = x[5] #unique
    #print tx_signature
    digit_last = (re.findall("(\d)", block_hash))[-1]
    #print digit_last
    if (int(digit_last) in player) and (bet_amount <= bet_max):
        #print "player wins"
        won_count = won_count + 1

        if openfield == base64.b64decode("payout for "+tx_signature):
            print "paid already"
        else:
            payout_missing.append(x)
    else:
        #print "bank wins"
        lost_count = lost_count + 1

print lost_count
print won_count

for y in payout_missing:
    payout_address = y[2]
    print payout_address
    bet_amount = float(y[4])
    tx_signature = y[5]  # unique

    #create transactions for missing payouts
    timestamp = str(time.time())
    transaction = (timestamp,address,payout_address,str(float(bet_amount+bet_amount)),base64.b64encode("payout for "+tx_signature))
    print transaction
    #create transactions for missing payouts
conn.close()

