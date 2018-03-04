import sqlite3
import log
import time, random
from decimal import *

def fee_calculate(openfield):
    fee = Decimal("0.01") + (Decimal(len(openfield)) / 100000)  # 0.01 dust
    if "token:issue:" in openfield:
        fee = Decimal(fee) + Decimal(10)
    if "alias=" in openfield:
        fee = Decimal(fee) + Decimal(1)
    return fee

def execute(cursor, query):
    """Secure execute for slow nodes"""
    while True:
        try:
            cursor.execute(query)
            break
        except Exception as e:
            app_log.warning("Database query: {} {}".format(cursor, query))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(random.uniform(0, 1))
    return cursor


def execute_param(cursor, query, param):
    """Secure execute w/ param for slow nodes"""
    while True:
        try:
            cursor.execute(query, param)
            break
        except Exception as e:
            app_log.warning("Database query: {} {} {}".format(cursor, query, param))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(random.random())
    return cursor

def masternodes_update(file, mode, app_log):
    if mode not in ("normal","reindex"):
        raise ValueError ("Wrong value for masternodes_update function")

    mas = sqlite3.connect(file)
    mas.text_factory = str
    m = mas.cursor()
    m.execute("CREATE TABLE IF NOT EXISTS masternodes (block_height INTEGER, timestamp NUMERIC, address, delegates, ips, txid)")
    mas.commit()

    if mode == "reindex":
        app_log.warning("Masternodes database will be reindexed")
        m.execute("DELETE FROM masternodes")
        mas.commit()

    conn = sqlite3.connect('static/ledger.db')
    conn.text_factory = str
    c = conn.cursor()

    c.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
    block_last = c.fetchone()[0] #get last block
    print ("block_last",block_last)

    i = 0

    reg_phase_end = block_last

    while True:
        if reg_phase_end % 10000 != 0:
            reg_phase_end = block_last - i
            i = i + 1
        else:
            break

    reg_phase_end = block_last#hack FOR TESTING ONLY

    reg_phase_start = reg_phase_end - 10000
    print("reg_phase_start", reg_phase_start)
    print("reg_phase_end", reg_phase_end)

    for row in c.execute("SELECT block_height, timestamp, address, openfield, block_hash FROM transactions WHERE block_height >= ? AND block_height <= ? AND openfield LIKE ?", (reg_phase_start,) + (reg_phase_end,) + ("masternode:" + '%',)):
        block_height = row[0]
        timestamp = row[1]
        address = row[2]
        openfield_split = row[3].split(":")
        txid = row[4][:56]

        delegates = openfield_split[2].split(",")
        ips = openfield_split[3].split(",")
        print(openfield_split)
        print(delegates)
        print(ips)

        try:
            m.execute("SELECT * from masternodes WHERE address = ?", (address,))
            dummy = m.fetchall()[0] #check for uniqueness
            app_log.warning("Masternode already registered: {}".format(txid))
        except:
            m.execute("INSERT INTO masternodes VALUES (?, ?, ?, ?, ?, ?)",(block_height, timestamp, address, str(delegates), str(ips), txid))
            mas.commit()


    c.close()
    m.close()



if __name__ == "__main__":
    app_log = log.log("masternodes.log", "WARNING", "yes")
    masternodes_update("index.db","normal",app_log)
    #masternode:delegates:x,y,z:ips