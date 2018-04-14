

import options
import os, sqlite3, sys,  time, random
from decimal import *
from quantizer import *

print(getcontext())
#getcontext().prec=120
#getcontext().rounding=ROUND_05UP
#getcontext().Emin=-100
#getcontext().Emax=100

config = options.Get()
config.read()

genesis_conf = config.genesis_conf
ledger_path_conf = config.ledger_path_conf
hyper_path_conf = config.hyper_path_conf


def db_h_define():
    hdd = sqlite3.connect(ledger_path_conf, timeout=1)
    hdd.text_factory = str
    h = hdd.cursor()
    hdd.execute("PRAGMA page_size = 4096;")
    return hdd, h


def db_h2_define():
    hdd2 = sqlite3.connect(hyper_path_conf, timeout=1)
    hdd2.text_factory = str
    h2 = hdd2.cursor()
    hdd2.execute("PRAGMA page_size = 4096;")
    return hdd2, h2


def commit(cursor):
    """Secure commit for slow nodes"""
    while True:
        try:
            cursor.commit()
            break
        except Exception as e:
            print("Database cursor: {}".format(cursor))
            print("Database retry reason: {}".format(e))

def execute(cursor, query):
    """Secure execute for slow nodes"""
    while True:
        try:
            cursor.execute(query)
            break
        except Exception as e:
            print("Database query: {} {}".format(cursor, query))
            print("Database retry reason: {}".format(e))

    return cursor


def execute_param(cursor, query, param):
    """Secure execute w/ param for slow nodes"""
    while True:
        try:
            cursor.execute(query, param)
            break
        except Exception as e:
            print("Database query: {} {} {}".format(cursor, query, param))
            print("Database retry reason: {}".format(e))
            time.sleep(random.random())
    return cursor

def balance_from_cursor(cursor, address):
    credit = Decimal("0")
    debit = Decimal("0")
    for entry in execute_param(cursor, "SELECT amount,reward FROM transactions WHERE recipient = ? ",(address, )):
        try:
            #result = cursor.fetchall()
            credit = credit + quantize_eight(entry[0]) + quantize_eight(entry[1])
            #print (result)
            credit = 0 if credit is None else credit
        except Exception as e:
            credit = 0
        #print (credit)


    for entry in execute_param(cursor, "SELECT amount,fee FROM transactions WHERE address = ? ",(address, )):
        try:
            # result = cursor.fetchall()
            debit = debit + quantize_eight(entry[0]) + quantize_eight(entry[1])
            # print (result)
            debit = 0 if debit is None else debit
        except Exception as e:
            debit = 0
        # print (debit)

    return quantize_eight(credit-debit)

def check(addresses):
    global ERRORS
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



if __name__ == "__main__":
    # Hyper
    hdd2, h2 = db_h2_define()
    # Ledger
    hdd, h = db_h_define()
    ERRORS = 0
    print("Selecting all addresses from full ledger for errors")
    execute(h, ("SELECT distinct(recipient) FROM transactions group by recipient;"))
    result = h.fetchall()
    check(result)
    print("Done, {} Errors.".format(ERRORS))