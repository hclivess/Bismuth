import options
import os, sqlite3, sys,  time, random
from decimal import *

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
    execute_param(cursor, "SELECT sum(amount)+sum(reward) FROM transactions WHERE recipient = ? ",
        (address, ))
    try:
        credit = Decimal(cursor.fetchone()[0]).quantize(Decimal('0.00000000'))
    except Exception as e:
        credit = 0

    execute_param(cursor, "SELECT sum(amount)+sum(fee) FROM transactions WHERE address = ? ",
        (address, ))
    try:
        debit = Decimal(cursor.fetchone()[0]).quantize(Decimal('0.00000000'))
    except Exception as e:
        debit = 0

    # limiting to .6f to ignore small round errors
    res =  "{:0.8f}".format(credit-debit)
    if res == '-0.00000000':
        res = '0.00000000'
    return res

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