"""
Test of various balance check methods
"""

import sqlite3
import time
import sys

# custom modules
sys.path.append('../')
from quantizer import *

LEDGER_PATH = '../static/ledger.db'
HYPER_PATH = '../static/hyper.db'

ADDRESSES = ["edf2d63cdf0b6275ead22c9e6d66aa8ea31dc0ccb367fad2e7c08a25",
             "da8a39cc9d880cd55c324afc2f9596c64fac05b8d41b3c9b6c481b4e",
             "e13e79dc7e4b8265d7cdafe31819939fcce98abc2c7662f7fb53fa38",
             "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed"]

# subset , faster.
ADDRESSES = ["edf2d63cdf0b6275ead22c9e6d66aa8ea31dc0ccb367fad2e7c08a25",
             "da8a39cc9d880cd55c324afc2f9596c64fac05b8d41b3c9b6c481b4e"]

"""
ledger_balance_node: 61.1779580116272s
{'edf2d63cdf0b6275ead22c9e6d66aa8ea31dc0ccb367fad2e7c08a25': Decimal('4024805.98934682'), 'da8a39cc9d880cd55c324afc2f9596c64fac05b8d41b3c9b6c481b4e': Decimal('4679.79590757'), 'e13e79dc7e4b8265d7cdafe31819939fcce98abc2c7662f7fb53fa38': Decimal('855.83117533'), '4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed': Decimal('528412.64728875')}

ledger_balance2: 36.27072238922119s
{'edf2d63cdf0b6275ead22c9e6d66aa8ea31dc0ccb367fad2e7c08a25': Decimal('4024805.98934682'), 'da8a39cc9d880cd55c324afc2f9596c64fac05b8d41b3c9b6c481b4e': Decimal('4679.79590757'), 'e13e79dc7e4b8265d7cdafe31819939fcce98abc2c7662f7fb53fa38': Decimal('855.83117533'), '4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed': Decimal('528412.64728875')}

ledger_balance3: 3.6478216648101807s
{'edf2d63cdf0b6275ead22c9e6d66aa8ea31dc0ccb367fad2e7c08a25': Decimal('4024805.98934682'), 'da8a39cc9d880cd55c324afc2f9596c64fac05b8d41b3c9b6c481b4e': Decimal('4679.79590757'), 'e13e79dc7e4b8265d7cdafe31819939fcce98abc2c7662f7fb53fa38': Decimal('855.83117533'), '4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed': Decimal('528412.64728875')}

"""


def db_h_define(path):
    hdd = sqlite3.connect(path, timeout=1)
    hdd.text_factory = str
    h = hdd.cursor()
    hdd.execute("PRAGMA page_size = 4096;")
    return hdd, h


def execute_param(cursor, query, param):
    """Secure execute w/ param for slow nodes"""
    while True:
        try:
            cursor.execute(query, param)
            break
        except sqlite3.InterfaceError as e:
            print("Database query to abort: {} {} {}".format(cursor, query, param))
            print("Database abortion reason: {}".format(e))
            break
        except sqlite3.IntegrityError as e:
            print("Database query to abort: {} {}".format(cursor, query))
            print("Database abortion reason: {}".format(e))
            break
        except Exception as e:
            print("Database query: {} {} {}".format(cursor, query, param))
            print("Database retry reason: {}".format(e))
            time.sleep(1)
    return cursor


def ledger_balance_node(address, c):

    credit_ledger = Decimal("0")
    for entry in execute_param(c, "SELECT amount FROM transactions WHERE recipient = ?;", (address,)):
        credit_ledger = quantize_eight(credit_ledger) + quantize_eight(entry[0])
        credit_ledger = 0 if credit_ledger is None else quantize_eight(credit_ledger)

    debit_ledger = Decimal("0")
    for entry in execute_param(c, "SELECT amount FROM transactions WHERE address = ?;", (address,)):
        debit_ledger = quantize_eight(debit_ledger) + quantize_eight(entry[0])
        debit_ledger = 0 if debit_ledger is None else quantize_eight(debit_ledger)

    fees = Decimal("0")
    for entry in execute_param(c, "SELECT fee FROM transactions WHERE address = ?;", (address,)):
        try:
            fees = quantize_eight(fees) + quantize_eight(entry[0])
            fees = 0 if fees is None else fees
        except:
            fees = 0

    rewards = Decimal("0")
    for entry in execute_param(c, "SELECT reward FROM transactions WHERE recipient = ?;", (address,)):
        try:
            rewards = quantize_eight(rewards) + quantize_eight(entry[0])
            rewards = 0 if rewards is None else rewards
        except:
            rewards = 0

    return quantize_eight(credit_ledger - debit_ledger + rewards - fees)


def ledger_balance2(address, c):
    # 2 sql requests only instead of 4 + more rational quantize use.
    credit_ledger = Decimal(0)
    for entry in execute_param(c, "SELECT amount, reward FROM transactions WHERE recipient = ?;", (address,)):
        credit_ledger += quantize_eight(entry[0]) + quantize_eight(entry[1])

    debit_ledger = Decimal(0)
    for entry in execute_param(c, "SELECT amount, fee FROM transactions WHERE address = ?;", (address,)):
        debit_ledger += quantize_eight(entry[0]) + quantize_eight(entry[1])

    return quantize_eight(credit_ledger - debit_ledger)


def ledger_balance3(address, c, cache):
    # Many heavy blocks are pool payouts, same address.
    # Cache pre_balance instead of recalc for every tx
    if address in cache:
        return cache[address]
    credit_ledger = Decimal(0)
    for entry in execute_param(c, "SELECT amount, reward FROM transactions WHERE recipient = ?;", (address,)):
        credit_ledger += quantize_eight(entry[0]) + quantize_eight(entry[1])

    debit_ledger = Decimal(0)
    for entry in execute_param(c, "SELECT amount, fee FROM transactions WHERE address = ?;", (address,)):
        debit_ledger += quantize_eight(entry[0]) + quantize_eight(entry[1])

    cache[address] = quantize_eight(credit_ledger - debit_ledger)
    return cache[address]


if __name__ == "__main__":
    conn, c = db_h_define(LEDGER_PATH)
    start_time = time.time()
    balance = {}
    for i in range(10):  # Simulate a block with 10 tx from that address
        for address in ADDRESSES:
            balance[address] = ledger_balance_node(address, c)
    run_time = time.time() - start_time
    print("ledger_balance_node: {}s".format(run_time))
    print(balance)
    start_time = time.time()
    balance = {}
    for i in range(10):
        for address in ADDRESSES:
            balance[address] = ledger_balance2(address, c)
    run_time = time.time() - start_time
    print("\nledger_balance2: {}s".format(run_time))
    print(balance)

    start_time = time.time()
    balance = {}
    for i in range(10):
        for address in ADDRESSES:
            balance[address] = ledger_balance3(address, c, balance)
    run_time = time.time() - start_time
    print("\nledger_balance3: {}s".format(run_time))
    print(balance)

    conn, c = db_h_define(HYPER_PATH)
    start_time = time.time()
    balance = {}
    for i in range(10):
        for address in ADDRESSES:
            balance[address] = ledger_balance_node(address, c)
    run_time = time.time() - start_time
    print("hyper_balance_node: {}s".format(run_time))
    print(balance)
    start_time = time.time()
    balance = {}
    for i in range(10):
        for address in ADDRESSES:
            balance[address] = ledger_balance2(address, c)
    run_time = time.time() - start_time
    print("\nhyper_balance2: {}s".format(run_time))
    print(balance)

    start_time = time.time()
    balance = {}
    for i in range(10):
        for address in ADDRESSES:
            balance[address] = ledger_balance3(address, c, balance)
    run_time = time.time() - start_time
    print("\nhyper_balance3: {}s".format(run_time))
    print(balance)
