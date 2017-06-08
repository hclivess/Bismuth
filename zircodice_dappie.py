import sqlite3, keys, base64

from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
import re
import time


def percentage(percent, whole):
  return (percent * whole) / 100.0

(key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read()

confirmations = 10
run = 0
bet_max = 100
checked = []
processed = []

conn = sqlite3.connect('static/ledger.db')
conn.text_factory = str
c = conn.cursor()

while True:
    if run % 500 == 0:
        del checked[:] #prevent overflow
        del processed[:] #prevent overflow
        run = 0 #reset runs
    run = run + 1

    # confirmations
    passed = 0
    while passed == 0:
        try:
            c.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1")
            block_height_last = c.fetchone()[0]
            # confirmations

            c.execute("SELECT * FROM transactions WHERE (openfield = ? OR openfield = ?) and recipient = ? and block_height <= ? ORDER BY block_height DESC LIMIT 500",("odd",)+("even",)+(address,)+(block_height_last-confirmations,))
            result_bets = c.fetchall()
            passed = 1
        except sqlite3.OperationalError, e:
            print "Database locked, retrying"
            pass

    won_count = 0
    lost_count = 0
    paid_count = 0
    not_paid_count = 0

    payout_missing = []

    for x in result_bets:
        if x not in checked:
            checked.append(x)

            openfield = str(x[11])
            if openfield == "even":
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
            if (int(digit_last) in player) and (bet_amount <= bet_max) and (bet_amount != 0):
                # print "player wins"
                won_count = won_count + 1

                passed = 0
                while passed == 0:
                    try:
                        c.execute("SELECT * FROM transactions where openfield = ? OR openfield = ?;",("payout for " + tx_signature[:8],("payout for " + tx_signature)))
                        result_in_ledger = c.fetchone()[0]
                        print "Payout transaction already in the ledger for {}".format(tx_signature[:8])
                        paid_count = paid_count + 1
                        passed = 1

                    except sqlite3.OperationalError, e:
                        print "Database locked, retrying"
                        pass

                    except Exception as e:
                        #print e
                        print "Appending tx to the payout list for {}".format(tx_signature[:8])
                        payout_missing.append(x)
                        not_paid_count = not_paid_count + 1
                        passed = 1

            else:
                # print "bank wins"
                lost_count = lost_count + 1

    print "Run: " + str(run)
    print "Total client lost rounds: " + str(lost_count)
    print "Total client won rounds: " + str(won_count)
    print "Already paid out x times: " + str(paid_count)
    print "Not paid out yet x times: " + str(not_paid_count)

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

            payout_amount = float(bet_amount * 2) - percentage(1, bet_amount)
            payout_openfield = "payout for " + tx_signature[:8]
            payout_keep = 0
            fee = float(0.01 + (float(payout_amount) * 0.001) + (float(len(payout_openfield)) / 100000) + (float(payout_keep) / 10))  # 0.1% + 0.01 dust

            transaction = (str(timestamp), str(address), str(payout_address), '%.8f' % (payout_amount-fee), str(payout_keep), str(payout_openfield))
            print transaction

            h = SHA.new(str(transaction))
            signer = PKCS1_v1_5.new(key)
            signature = signer.sign(h)
            signature_enc = base64.b64encode(signature)
            #print("Encoded Signature: {}".format(signature_enc))

            verifier = PKCS1_v1_5.new(key)
            if verifier.verify(h, signature) == True:
                print("Signature OK")

            mempool = sqlite3.connect('mempool.db')
            mempool.text_factory = str
            m = mempool.cursor()

            passed = 0
            while passed == 0:
                try:
                    m.execute("SELECT * FROM transactions WHERE openfield = ?;",("payout for " + tx_signature[:8],))
                    result_in_mempool = m.fetchone()[0]
                    print "Payout transaction already in the mempool"
                    passed = 1
                except sqlite3.OperationalError, e:
                    print "Database locked, retrying"
                    pass
                except:
                    m.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", (
                    str(timestamp), str(address), str(payout_address), '%.8f' % (float(payout_amount-fee)), str(signature_enc), str(public_key_hashed), "0",
                    str("payout for " + tx_signature[:8])))
                    mempool.commit()  # Save (commit) the changes
                    mempool.close()
                    print "Mempool updated with a payout transaction for {}".format(tx_signature[:8])
                    passed = 1


                # create transactions for missing payouts
    time.sleep(15)
conn.close()