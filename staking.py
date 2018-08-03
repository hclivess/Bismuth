#todo: make registrations produce mirror hashes
#todo: make sure registrations newer than latest block are ignored
#todo: rollbacks inside node; make sure delagete/ip is only allowed characters

#operation: staking:register

import sqlite3
import log
from quantizer import *
import mempool as mp
from hashlib import blake2b
import re

def percentage(percent, whole):
    return ((Decimal(percent) * Decimal(whole)) / 100)

def execute_param(cursor, query, param):
    """Secure execute w/ param for slow nodes"""
    while True:
        try:
            cursor.execute(query, param)
            break
        except Exception as e:
            app_log.warning("Database query: {} {} {}".format(cursor, query, param))
            app_log.warning("Database retry reason: {}".format(e))
    return cursor

def balanceget_at_block(balance_address,block, h3):
    # verify balance

    credit_ledger = Decimal ("0")
    for entry in execute_param (h3, ("SELECT amount FROM transactions WHERE block_height <= ? AND block_height >= ? AND recipient = ?;"), (block, -block, balance_address,)):
        try:
            credit_ledger = quantize_eight (credit_ledger) + quantize_eight (entry[0])
            credit_ledger = 0 if credit_ledger is None else credit_ledger
        except:
            credit_ledger = 0

    fees = Decimal ("0")
    debit_ledger = Decimal ("0")

    for entry in execute_param (h3, ("SELECT fee, amount FROM transactions WHERE block_height <= ? AND block_height >= ? AND address = ?;"), (block, -block, balance_address,)):
        try:
            fees = quantize_eight (fees) + quantize_eight (entry[0])
            fees = 0 if fees is None else fees
        except:
            fees = 0

        try:
            debit_ledger = debit_ledger + Decimal (entry[1])
            debit_ledger = 0 if debit_ledger is None else debit_ledger
        except:
            debit_ledger = 0

    debit = quantize_eight (debit_ledger)

    rewards = Decimal ("0")
    for entry in execute_param (h3, ("SELECT reward FROM transactions WHERE block_height <= ? AND block_height >= ? AND recipient = ?;"), (block, -block, balance_address,)):
        try:
            rewards = quantize_eight (rewards) + quantize_eight (entry[0])
            rewards = 0 if rewards is None else rewards
        except:
            rewards = 0

    balance = quantize_eight (credit_ledger - debit - fees + rewards)
    # app_log.info("Mempool: Projected transction address balance: " + str(balance))
    return str(balance) #, str (credit_ledger), str (debit), str (fees), str (rewards)

def check_db(index,index_cursor):
    index_cursor.execute("CREATE TABLE IF NOT EXISTS staking (block_height INTEGER, timestamp NUMERIC, address, balance)")
    index.commit()

def staking_update(conn,c,index,index_cursor, mode, reg_phase_end, app_log):
    """update register of staking based on the current phase (10000 block intervals)"""
    if mode not in ("normal","reindex"):
        raise ValueError ("Wrong value for staking_update function")

    check_db(index,index_cursor)

    if mode == "reindex":
        app_log.warning("staking database will be reindexed")
        index_cursor.execute("DELETE FROM staking")
        index.commit()

    reg_phase_start = reg_phase_end - 10000
    app_log.warning("reg_phase_start: {}".format(reg_phase_start))
    app_log.warning("reg_phase_end: {}".format(reg_phase_end))

    c.execute("SELECT block_height, timestamp, address, recipient,operation, openfield FROM transactions WHERE block_height >= ? AND block_height <= ? AND operation = ? ORDER BY block_height, timestamp LIMIT 100", (reg_phase_start, reg_phase_end, "staking:register",))
    results = c.fetchall() #more efficient than "for row in"

    for row in results:
        try:
            block_height = row[0]
            timestamp = row[1]
            address = row[2]

            try:
                index_cursor.execute("SELECT * from staking WHERE address = ?", (address,))
                dummy = index_cursor.fetchall()[0] #check for uniqueness
                app_log.warning("staking already registered: {}".format(address))
            except:
                app_log.warning("address: {}".format(address))
                balance = balanceget_at_block(address, reg_phase_end, c)

                if quantize_eight(balance) >= 10000:
                    index_cursor.execute("INSERT INTO staking VALUES (?, ?, ?, ?)", (block_height, timestamp, address, balance))
                    index.commit()

                    app_log.warning ("Staking added: {} {}".format (block_height, address))
                else:
                    app_log.warning("Insufficient balance for staking")
        except Exception as e:
            app_log.warning("Staking registration ran into the following issue: {}".format(e))

    return reg_phase_start, reg_phase_end


def mirror_hash_generate(c):
    # new hash
    c.execute("SELECT * FROM transactions WHERE block_height = (SELECT block_height FROM transactions ORDER BY block_height ASC LIMIT 1)")
    result = c.fetchall()
    mirror_hash = blake2b(str (result).encode(), digest_size=20).hexdigest()
    return mirror_hash
    # new hash

def staking_payout(conn,c,index,index_cursor,block_height,timestamp,app_log):
    "payout, to be run every 10k blocks"

    index_cursor.execute("SELECT * FROM staking")
    staking = index_cursor.fetchall()
    app_log.warning("staking: {}".format(staking))
    staking_total = len(staking)

    mirror_hash = mirror_hash_generate(c)

    for staking in staking:
        block_staking = staking[0]
        if block_staking <= block_height:
            address = staking[2]
            balance_savings = staking[3]
            app_log.warning("balance_savings: {}".format(balance_savings))
            stake = str(quantize_eight(percentage(100/staking_total,balance_savings)))
            app_log.warning("stake: {}".format(stake))

            try:
                c.execute ("SELECT * from transactions WHERE block_height = ? AND recipient = ?", (-block_height,address,))
                dummy = c.fetchall ()[0]  # check for uniqueness
                app_log.warning ("staking payout already processed: {} {}".format(block_height,address))

            except:
                """skip direct bis payouts
                c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(-block_height,timestamp,"staking",address,stake,"0","0",mirror_hash,"0","0","mnpayout","0"))
                conn.commit()
                app_log.warning ("staking payout added: {} {}".format (block_height, address))
                """

                #fuel
                stake_int = int(float(stake))
                if stake_int < 1:
                    stake_int = 1

                c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(-block_height,timestamp,"staking",address,"0","0","0",mirror_hash,"0","0","token:transfer","fuel:{}".format(stake_int)))
                conn.commit()
                app_log.warning ("Staking fuel payout added: {} {}".format (block_height, address))
                #fuel
        else:
            app_log.warning("staking is registered ahead of current block")

def staking_revalidate(conn,c,index,index_cursor,block,app_log):
    "remove nodes that removed balance, to be run every 10k blocks"

    index_cursor.execute("SELECT * FROM staking")
    staking = index_cursor.fetchall()

    for staking in staking:
        app_log.warning (staking)
        address = staking[2]
        app_log.warning ("address: {}".format(address))
        balance_savings = staking[3]
        app_log.warning("balance_savings: {}".format(balance_savings))
        balance = balanceget_at_block (address, block, c)
        app_log.warning ("balance: {}".format(balance))

        if quantize_eight(balance) < 10000:
            index_cursor.execute("DELETE FROM staking WHERE address = ?",(address,))
            index.commit()
        else: #update balance
            index_cursor.execute("UPDATE staking SET balance = ? WHERE address = ?",(balance,address))
            index.commit ()
            app_log.warning("staking balance updated from {} to {} for {}".format(balance_savings,balance,address))

if __name__ == "__main__":


    import options
    config = options.Get ()
    config.read ()


    app_log = log.log ("solvency.log", "WARNING", True)
    mp.MEMPOOL = mp.Mempool (app_log,config,None,False)


    conn = sqlite3.connect('static/test.db')
    conn.text_factory = str
    c = conn.cursor()

    index = sqlite3.connect("static/index_test.db")
    index.text_factory = str
    index_cursor = index.cursor()

    address = "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed"
    staking_update(conn, c,index,index_cursor, "normal", 736600, app_log)
    staking_payout(conn, c,index,index_cursor,736600, 1525304875, app_log)
    staking_revalidate (conn, c,index, index_cursor,736600, app_log)