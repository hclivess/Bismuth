#this file is marginally dynamic, make sure you know what you run it against

import tarfile
import sys
import sqlite3
from decimal import *
from quantizer import *
import process_search


def balance_from_cursor(cursor, address):
    credit = Decimal("0")
    debit = Decimal("0")
    for entry in cursor.execute("SELECT amount,reward FROM transactions WHERE recipient = ? ",(address, )):
        try:
            #result = cursor.fetchall()
            credit = credit + quantize_eight(entry[0]) + quantize_eight(entry[1])
            #print (result)
            credit = 0 if credit is None else credit
        except Exception as e:
            credit = 0
        #print (credit)


    for entry in cursor.execute("SELECT amount,fee FROM transactions WHERE address = ? ",(address, )):
        try:
            # result = cursor.fetchall()
            debit = debit + quantize_eight(entry[0]) + quantize_eight(entry[1])
            # print (result)
            debit = 0 if debit is None else debit
        except Exception as e:
            debit = 0
        # print (debit)

    return quantize_eight(credit-debit)

def errors():
    hdd = sqlite3.connect("ledger.db", timeout=1)
    hdd.text_factory = str
    h = hdd.cursor()

    hdd2 = sqlite3.connect("hyper.db", timeout=1)
    hdd2.text_factory = str
    h2 = hdd2.cursor()

    ERRORS = 0
    print ("Selecting all addresses from full ledger for errors")
    h.execute ("SELECT distinct(recipient) FROM transactions group by recipient;")
    addresses = h.fetchall ()

    for address in addresses:
        address = address[0]
        balance1 = balance_from_cursor(h, address)
        balance2 = balance_from_cursor(h2, address)
        if (balance1 == balance2):
            check = '  Ok'
        else:
            check = '> Ko'
            ERRORS += 1

        if address.lower() != address or len(address) != 56 and (balance1 or balance2) != 0:
            print (address,'> you dun fukt it up')


        print(check, address, balance1, balance2)

        if (Decimal(balance1) < 0 or Decimal(balance2) < 0):
            print(address,balance1,balance2)


    print("Done, {} Errors.".format(ERRORS))

    if ERRORS > 0:
        return_value = True
    else:
        return_value = False

    return return_value



if errors():
    print("There were errors in the hyperblocks, cannot continue")
else:

    if not process_search.proccess_presence ("node.py"):
        files = ["ledger.db-wal","ledger.db-shm","ledger.db","hyper.db-shm", "hyper.db-wal", "hyper.db", "index.db"]

        tar = tarfile.open("ledger.tar.gz", "w:gz")

        for file in files:
            try:
                print ("Compressing", file)
                tar.add(file, arcname=file)
            except:
                "Error compressing {}".format(file)

        print("Compression finished for", files)
        tar.close()

    else:
        print ("Node is running, cannot continue")

input("Press any key to continue")


