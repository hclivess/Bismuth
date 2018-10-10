version = "whatever"
import time
from quantizer import *
import shutil
import os
import sqlite3

hyper_recompress_conf = True
full_ledger = True


def ledger_compress(ledger_path_conf, hyper_path_conf):
    """conversion of normal blocks into hyperblocks from ledger.db or hyper.db to hyper.db"""
    try:

        # if os.path.exists(hyper_path_conf+".temp"):
        #    os.remove(hyper_path_conf+".temp")
        #    print("Status: Removed old temporary hyperblock file")
        #    time.sleep(100)

        if os.path.exists(hyper_path_conf):

            if full_ledger:
                # cross-integrity check
                hdd = sqlite3.connect(ledger_path_conf, timeout=1)
                hdd.text_factory = str
                h = hdd.cursor()
                h.execute("SELECT max(block_height) FROM transactions")
                hdd_block_last = h.fetchone()[0]
                hdd.close()

                hdd2 = sqlite3.connect(hyper_path_conf, timeout=1)
                hdd2.text_factory = str
                h2 = hdd2.cursor()
                h2.execute("SELECT max(block_height) FROM transactions")
                hdd2_block_last = h2.fetchone()[0]
                hdd2.close()
                # cross-integrity check

                if hdd_block_last == hdd2_block_last and hyper_recompress_conf:  # cross-integrity check
                    ledger_path_conf = hyper_path_conf  # only valid within the function, this temporarily sets hyper.db as source
                    print("Status: Recompressing hyperblocks (keeping full ledger)")
                    recompress = True
                elif hdd_block_last == hdd2_block_last and hyper_recompress_conf:
                    print("Status: Hyperblock recompression skipped")
                    recompress = False
                else:
                    print(
                        "Status: Cross-integrity check failed, hyperblocks will be rebuilt from full ledger")
                    recompress = True
            else:
                if hyper_recompress_conf:
                    print("Status: Recompressing hyperblocks (without full ledger)")
                    recompress = True
                else:
                    print("Status: Hyperblock recompression skipped")
                    recompress = False
        else:
            print("Status: Compressing ledger to Hyperblocks")
            recompress = True

        if recompress:
            depth = 15000  # REWORK TO REFLECT TIME INSTEAD OF BLOCKS

            # if os.path.exists(ledger_path_conf + '.temp'):
            #    os.remove(ledger_path_conf + '.temp')

            if full_ledger:
                shutil.copy(ledger_path_conf, ledger_path_conf + '.temp')
                hyper = sqlite3.connect(ledger_path_conf + '.temp')
            else:
                shutil.copy(hyper_path_conf, ledger_path_conf + '.temp')
                hyper = sqlite3.connect(ledger_path_conf + '.temp')

            hyper.text_factory = str
            hyp = hyper.cursor()

            addresses = []

            hyp.execute("UPDATE transactions SET address = 'Hypoblock' WHERE address = 'Hyperblock'")

            hyp.execute("SELECT max(block_height) FROM transactions")
            db_block_height = int(hyp.fetchone()[0])
            depth_specific = db_block_height - depth

            hyp.execute("SELECT distinct(recipient) FROM transactions WHERE (block_height < ?) ORDER BY block_height;", (depth_specific,))  # new addresses will be ignored until depth passed
            unique_addressess = hyp.fetchall()

            for x in set(unique_addressess):
                credit = Decimal("0")
                for entry in hyp.execute("SELECT amount,reward FROM transactions WHERE (recipient = ? AND block_height < ?);", (x[0],) + (depth_specific,)):
                    try:
                        credit = quantize_eight(credit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
                        credit = 0 if credit is None else credit
                    except Exception as e:
                        credit = 0

                debit = Decimal("0")
                for entry in hyp.execute("SELECT amount,fee FROM transactions WHERE (address = ? AND block_height < ?);", (x[0],) + (depth_specific,)):
                    try:
                        debit = quantize_eight(debit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
                        debit = 0 if debit is None else debit
                    except Exception as e:
                        debit = 0

                end_balance = quantize_eight(credit - debit)

                # app_log.info("Address: "+ str(x))
                # app_log.info("Credit: " + str(credit))
                # app_log.info("Debit: " + str(debit))
                # app_log.info("Fees: " + str(fees))
                # app_log.info("Rewards: " + str(rewards))
                # app_log.info("Balance: " + str(end_balance))

                # print(x[0],end_balance)

                if end_balance > 0:
                    timestamp = str(time.time())
                    hyp.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                        depth_specific - 1, timestamp, "Hyperblock", x[0], str(end_balance), "0", "0", "0", "0",
                        "0", "0", "0"))
            hyper.commit()

            hyp.execute("DELETE FROM transactions WHERE block_height < ? AND address != 'Hyperblock';", (depth_specific,))
            hyper.commit()

            hyp.execute("DELETE FROM misc WHERE block_height < ?;", (depth_specific,))  # remove diff calc
            hyper.commit()

            hyp.execute("VACUUM")
            hyper.close()

            if os.path.exists(hyper_path_conf):
                os.remove(hyper_path_conf)  # remove the old hyperblocks

            os.rename(ledger_path_conf + '.temp', hyper_path_conf)

        if full_ledger == 0 and os.path.exists(ledger_path_conf) and "testnet" not in version:
            os.remove(ledger_path_conf)
            print("Removed full ledger and only kept hyperblocks")

    except Exception as e:
        raise ValueError("There was an issue converting to Hyperblocks: {}".format(e))

if __name__ == "__main__":
    ledger_compress("static/test.db", "static/test2.db")