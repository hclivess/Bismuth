import sqlite3
import hashlib
import base64
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
import re
import time
import os
from simplecrypt import decrypt
import getpass

# import keys
if not os.path.exists('privkey_encrypted.der'):
    password = ""
    key = RSA.importKey(open('privkey.der').read())
    private_key_readable = str(key.exportKey())
    # public_key = key.publickey()
else:
    password = getpass.getpass()
    encrypted_privkey = open('privkey_encrypted.der').read()
    decrypted_privkey = decrypt(password, base64.b64decode(encrypted_privkey))
    key = RSA.importKey(decrypted_privkey)  # be able to sign
    private_key_readable = str(key.exportKey())

public_key_readable = open('pubkey.der').read()
public_key_hashed = base64.b64encode(public_key_readable)
address = hashlib.sha224(public_key_readable).hexdigest()
# import keys

run = 0
bet_max = 1000
checked = []
processed = []

while True:
    if run % 500 == 0:
        del checked[:] #prevent overflow
        del processed[:] #prevent overflow
        run = 0 #reset runs
    run = run + 1
    conn = sqlite3.connect('static/ledger.db')
    conn.text_factory = str
    c = conn.cursor()
    c.execute("SELECT * FROM transactions WHERE openfield = '" + base64.b64encode("odd") + "' OR openfield = '" + base64.b64encode("even") + "' and recipient = '" + address + "' ORDER BY block_height DESC LIMIT 500")
    result_bets = c.fetchall()

    won_count = 0
    lost_count = 0
    paid_count = 0
    not_paid_count = 0

    payout_missing = []

    for x in result_bets:
        if x not in checked:
            checked.append(x)

            openfield = str(x[11])
            if base64.b64decode(openfield) == "odd":
                player = [0, 2, 4, 6, 8]
                bank = [1, 3, 5, 7, 9]
            else: #if even
                player = [1, 3, 5, 7, 9]
                bank = [0, 2, 4, 6, 8]

            bet_amount = float(x[4])
            block_hash = x[7]
            # print block_hash
            tx_signature = x[5]  # unique
            digit_last = (re.findall("(\d)", block_hash))[-1]
            # print digit_last
            if (int(digit_last) in player) and (bet_amount <= bet_max):
                # print "player wins"
                won_count = won_count + 1

                try:
                    c.execute("SELECT * FROM transactions where openfield = '" + base64.b64encode("payout for " + tx_signature) + "' ")
                    result_in_ledger = c.fetchone()[0]
                    print "Payout transaction already in the ledger for "+str(tx_signature)
                    paid_count = paid_count + 1

                except Exception as e:
                    print e
                    print "Appending tx to the payout list for "+str(tx_signature)
                    payout_missing.append(x)
                    not_paid_count = not_paid_count + 1

            else:
                # print "bank wins"
                lost_count = lost_count + 1

    print "Run: " + str(run)
    print "Total client lost rounds: " + str(lost_count)
    print "Total client won rounds: " + str(won_count)
    print "Already paid out x times: " + str(paid_count)
    print "Not paid out yet x times: " + str(not_paid_count)

    c.execute('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1')
    last_block_height = c.fetchone()[0]
    conn.close()

    for y in payout_missing:
        if y not in processed:
            processed.append(y) #can overflow

            payout_address = y[2]
            print payout_address
            bet_amount = float(y[4])
            tx_signature = y[5]  # unique
            #print y

            # create transactions for missing payouts
            timestamp = str(time.time())
            transaction = (timestamp, address, payout_address, str(float(bet_amount*2)),
                           base64.b64encode("payout for " + tx_signature))
            print transaction

            h = SHA.new(str(transaction))
            signer = PKCS1_v1_5.new(key)
            signature = signer.sign(h)
            signature_enc = base64.b64encode(signature)
            print("Encoded Signature: " + str(signature_enc))

            mempool = sqlite3.connect('mempool.db')
            mempool.text_factory = str
            m = mempool.cursor()

            try:
                m.execute("SELECT * FROM transactions WHERE openfield = '" + base64.b64encode("payout for " + tx_signature) + "' ")
                result_in_mempool = m.fetchone()[0]
                print "Payout transaction already in the mempool"
            except:
                m.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?)", (
                timestamp, address, payout_address, str(float(bet_amount*2)), signature_enc, public_key_hashed,
                base64.b64encode("payout for " + tx_signature)))
                mempool.commit()  # Save (commit) the changes
                mempool.close()
                print "Mempool updated with a payout transaction for "+str(tx_signature)


                # create transactions for missing payouts
    time.sleep(15)
