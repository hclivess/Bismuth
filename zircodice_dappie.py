import sqlite3, base64, options

from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
import time
from decimal import *
from random import randint
import essentials
from essentials import fee_calculate

block_anchor = 547989 #no payouts previous to this block

def roll(block_height, txid):
    roll = sqlite3.connect("roll.db")
    roll.text_factory = str
    r = roll.cursor()
    r.execute("CREATE TABLE IF NOT EXISTS transactions (block_height INTEGER, txid, rolled)")
    roll.commit()

    try:
        r.execute("SELECT rolled FROM transactions WHERE txid = ?",(txid,))
        roll_number = r.fetchone()[0]
    except:
        roll_number = (randint(0, 9))
        r.execute("INSERT INTO transactions VALUES (?,?,?)",(block_height, txid, roll_number))

    roll.commit()
    r.close()
    return roll_number

def percentage(percent, whole):
    return ((Decimal (percent) * Decimal(whole)) / 100)

#(key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read()
key, public_key_readable, private_key_readable, _, _, public_key_hashed, address = essentials.keys_load_new("wallet.der")


config = options.Get()
config.read()
debug_level = config.debug_level_conf
ledger_path_conf = config.ledger_path_conf
full_ledger = config.full_ledger_conf
ledger_path = config.ledger_path_conf
hyper_path = config.hyper_path_conf
terminal_output=config.terminal_output

confirmations = 5
run = 0
bet_max = 100
checked = []
processed = []

if full_ledger == 1:
    conn = sqlite3.connect(ledger_path)
else:
    conn = sqlite3.connect(hyper_path)
conn.text_factory = str
c = conn.cursor()

while True:
    if run % 500 == 0:
        del checked[:] #prevent overflow
        del processed[:] #prevent overflow
        run = 0 #reset runs
    run = run + 1

    # confirmations

    while True:
        try:
            c.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1")
            block_height_last = c.fetchone()[0]
            # confirmations

            c.execute("SELECT * FROM transactions WHERE (openfield = ? OR openfield = ?) and recipient = ? and block_height <= ? AND block_height > ? ORDER BY block_height DESC LIMIT 500",("odd",)+("even",)+(address,)+(block_height_last-confirmations,)+(block_anchor,))
            result_bets = c.fetchall()
            break
        except sqlite3.OperationalError as e:
            print ("Database locked, retrying")
            time.sleep(1)
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
            else: #if odd
                player = [1, 3, 5, 7, 9]
                bank = [0, 2, 4, 6, 8]

            bet_amount = float(x[4])
            block_hash = x[7]
            # print block_hash
            tx_signature = x[5]  # unique
            txid = x[5][:56]
            rolled = roll(x[0],txid)
            # print rolled
            if (int(rolled) in player) and (bet_amount <= bet_max) and (bet_amount != 0):
                # print "player wins"
                won_count = won_count + 1

                passed = 0
                while passed == 0:
                    try:
                        c.execute("SELECT * FROM transactions where (openfield = ? OR openfield = ?);",("payout for " + tx_signature[:8],("payout for " + tx_signature)))
                        result_in_ledger = c.fetchone()[0]
                        print ("Payout transaction already in the ledger for {}".format(tx_signature[:8]))
                        paid_count = paid_count + 1
                        passed = 1

                    except sqlite3.OperationalError as e:
                        print ("Database locked, retrying")
                        pass

                    except TypeError as e: #not there
                        #print e
                        print ("Appending tx to the payout list for {}".format(tx_signature[:8]))
                        payout_missing.append(x)
                        not_paid_count = not_paid_count + 1
                        passed = 1
                    except:
                        raise

            else:
                # print "bank wins"
                lost_count = lost_count + 1

    print ("Run: " + str(run))
    print ("Total client lost rounds: " + str(lost_count))
    print ("Total client won rounds: " + str(won_count))
    print ("Already paid out x times: " + str(paid_count))
    print ("Not paid out yet x times: " + str(not_paid_count))

    for y in payout_missing:
        if y not in processed:
            processed.append(y) #can overflow

            payout_address = y[2]
            print (payout_address)
            bet_amount = float(y[4])
            tx_signature = y[5]  # unique
            #print y

            # create transactions for missing payouts
            timestamp = '%.2f' % time.time()

            payout_amount = Decimal(bet_amount * 2) - percentage(5, bet_amount)
            payout_openfield = "payout for " + tx_signature[:8]
            payout_operation = 0
            fee = fee_calculate(payout_openfield)

            #float(0.01 + (float(payout_amount) * 0.001) + (float(len(payout_openfield)) / 100000) + (float(payout_keep) / 10))  # 0.1% + 0.01 dust

            transaction = (str(timestamp), str(address), str(payout_address), '%.8f' % float(payout_amount-fee), str(payout_operation), str(payout_openfield))  # this is signed
            print(transaction)

            h = SHA.new(str(transaction).encode("utf-8"))
            signer = PKCS1_v1_5.new(key)
            signature = signer.sign(h)
            signature_enc = base64.b64encode(signature)
            print("Encoded Signature: {}".format(signature_enc.decode()))

            verifier = PKCS1_v1_5.new(key)
            if verifier.verify(h, base64.b64decode(signature_enc)):
                print("Signature OK")

            mempool = sqlite3.connect('mempool.db')
            mempool.text_factory = str
            m = mempool.cursor()

            passed = 0
            while passed == 0:
                try:
                    m.execute("SELECT * FROM transactions WHERE openfield = ?;",("payout for " + tx_signature[:8],))
                    result_in_mempool = m.fetchone()[0]
                    print ("Payout transaction already in the mempool")
                    passed = 1
                except sqlite3.OperationalError as e:
                    print ("Database locked, retrying")
                    pass
                except TypeError: #not there
                    m.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", (str(timestamp), str(address), str(payout_address), '%.8f' % (float(payout_amount-fee)), str(signature_enc.decode("utf-8")), str(public_key_hashed.decode("utf-8")), "0", str("payout for " + tx_signature[:8])))
                    mempool.commit()  # Save (commit) the changes
                    mempool.close()
                    print ("Mempool updated with a payout transaction for {}".format(tx_signature[:8]))
                    passed = 1
                except:
                    raise


                # create transactions for missing payouts
    time.sleep(15)
conn.close()