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

player = [5, 6, 7, 8, 9]
bank = [0, 1, 2, 3, 4]
bet_max = 1000

while True:
    conn = sqlite3.connect('ledger.db')
    conn.text_factory = str
    c = conn.cursor()
    c.execute("select * from transactions where recipient = '" + address + "' and openfield = '" + base64.b64decode("bet") + "' ")
    result_bets = c.fetchall()

    won_count = 0
    lost_count = 0
    paid_count = 0
    not_paid_count = 0

    txs_winning = []
    payout_missing = []

    for x in result_bets:
        bet_amount = float(x[4])
        block_hash = x[7]
        # print block_hash
        tx_signature = x[5]  # unique
        # print tx_signature
        digit_last = (re.findall("(\d)", block_hash))[-1]
        # print digit_last
        if (int(digit_last) in player) and (bet_amount <= bet_max):
            # print "player wins"
            won_count = won_count + 1

            try:
                c.execute("SELECT * FROM transactions where openfield = '" + base64.b64encode(
                    "payout for " + tx_signature) + "' ")
                result_in_ledger = c.fetchone()[0]
                # print result_in_ledger
                print "Payout transaction already in the ledger"
                paid_count = paid_count + 1

            except:
                payout_missing.append(x)
                not_paid_count = not_paid_count + 1

        else:
            # print "bank wins"
            lost_count = lost_count + 1

    print "Total client lost rounds: " + str(lost_count)
    print "Total client won rounds: " + str(won_count)
    print "Already paid out x times: " + str(paid_count)
    print "Not paid out yet x times: " + str(not_paid_count)

    for y in payout_missing:
        payout_address = y[2]
        print payout_address
        bet_amount = float(y[4])
        tx_signature = y[5]  # unique

        # create transactions for missing payouts
        timestamp = str(time.time())
        transaction = (timestamp, address, payout_address, str(float(bet_amount + bet_amount)),
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
            m.execute("SELECT * FROM transactions where openfield = '" + base64.b64encode(
                "payout for " + tx_signature) + "' ")
            result_in_mempool = m.fetchone()[0]
            print result_in_mempool
            print "Payout transaction already in the mempool"
        except:
            m.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?)", (
            timestamp, address, payout_address, str(float(bet_amount + bet_amount)), signature_enc, public_key_hashed,
            base64.b64encode("payout for " + tx_signature)))
            mempool.commit()  # Save (commit) the changes
            mempool.close()
            print "Mempool updated with a payout transaction"


            # create transactions for missing payouts
    conn.close()
    time.sleep(30)
