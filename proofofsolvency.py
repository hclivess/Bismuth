import sqlite3
import log
from quantizer import *
import mempool as mp
from essentials import fee_calculate

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

def balanceget(balance_address, h3):
    # verify balance

    # app_log.info("Mempool: Verifying balance")
    # app_log.info("Mempool: Received address: " + str(balance_address))


    base_mempool = mp.MEMPOOL.fetchall ("SELECT amount, openfield FROM transactions WHERE address = ?;", (balance_address,))

    # include mempool fees

    debit_mempool = 0
    if base_mempool:
        for x in base_mempool:
            debit_tx = Decimal(x[0])
            fee = fee_calculate(x[1])
            debit_mempool = quantize_eight(debit_mempool + debit_tx + fee)
    else:
        debit_mempool = 0
    # include mempool fees

    credit_ledger = Decimal ("0")
    for entry in execute_param (h3, ("SELECT amount FROM transactions WHERE recipient = ?;"), (balance_address,)):
        try:
            credit_ledger = quantize_eight (credit_ledger) + quantize_eight (entry[0])
            credit_ledger = 0 if credit_ledger is None else credit_ledger
        except:
            credit_ledger = 0

    fees = Decimal ("0")
    debit_ledger = Decimal ("0")

    for entry in execute_param (h3, ("SELECT fee, amount FROM transactions WHERE address = ?;"), (balance_address,)):
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

    debit = quantize_eight (debit_ledger + debit_mempool)

    rewards = Decimal ("0")
    for entry in execute_param (h3, ("SELECT reward FROM transactions WHERE recipient = ?;"), (balance_address,)):
        try:
            rewards = quantize_eight (rewards) + quantize_eight (entry[0])
            rewards = 0 if rewards is None else rewards
        except:
            rewards = 0

    balance = quantize_eight (credit_ledger - debit - fees + rewards)
    balance_no_mempool = float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)
    # app_log.info("Mempool: Projected transction address balance: " + str(balance))
    return str (balance), str (credit_ledger), str (debit), str (fees), str (rewards), str(balance_no_mempool)



def masternodes_update(c,m, mode, reg_phase_end, app_log):
    """update register of masternodes based on the current phase (10000 block intervals)"""
    if mode not in ("normal","reindex"):
        raise ValueError ("Wrong value for masternodes_update function")


    m.execute("CREATE TABLE IF NOT EXISTS masternodes (block_height INTEGER, timestamp NUMERIC, address, balance, ip, delegate)")
    mas.commit()


    if mode == "reindex":
        app_log.warning("Masternodes database will be reindexed")
        m.execute("DELETE FROM masternodes")
        mas.commit()

    c.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
    block_last = c.fetchone()[0] #get last block
    print ("block_last",block_last)

    reg_phase_start = reg_phase_end - 10000
    print("reg_phase_start", reg_phase_start)
    print("reg_phase_end", reg_phase_end)

    c.execute("SELECT block_height, timestamp, address, recipient,operation, openfield FROM transactions WHERE block_height >= ? AND block_height <= ? AND command = ? ORDER BY block_height, timestamp LIMIT 100", (reg_phase_start,) + (reg_phase_end,) + ("masternode:register",))
    results = c.fetchall() #more efficient than "for row in"

    for row in results:
        block_height = row[0]
        timestamp = row[1]
        address = row[2]
        openfield_split = row[4].split(":")

        print("operation_split",openfield_split)
        ip = openfield_split[0] #openfield
        delegate = openfield_split[1]

        try:
            m.execute("SELECT * from masternodes WHERE address = ?", (address,))
            dummy = m.fetchall()[0] #check for uniqueness
            app_log.warning("Masternode already registered: {}".format(address))
        except:
            print("address",address)
            balance = balanceget(address, c)[5]

            if quantize_eight(balance) >= 10000:
                m.execute("INSERT INTO masternodes VALUES (?, ?, ?, ?, ?, ?)", (block_height, timestamp, address, balance, ip, delegate))
                mas.commit()
            else:
                app_log.warning("Insufficient balance for masternode")

    return reg_phase_start, reg_phase_end



def masternodes_payout(c,m,block_height,timestamp,app_log):
    "payout, to be run every 10k blocks"

    m.execute("SELECT * FROM masternodes")
    masternodes = m.fetchall()
    print("masternodes",masternodes)
    masternodes_total = len(masternodes)

    for masternode in masternodes:

        address = masternode[2]
        balance_savings = masternode[3]
        print("balance_savings",balance_savings)
        stake = str(quantize_eight(percentage(25/52/masternodes_total,balance_savings))) #divide by number of 10k blocks per year
        print("stake",stake)

        try:
            c.execute ("SELECT * from transactions WHERE block_height = ? AND recipient = ?", (-block_height,address,))
            dummy = c.fetchall ()[0]  # check for uniqueness
            app_log.warning ("Masternode operation already processed: {} {}".format(block_height,address))

        except:
            c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(-block_height,timestamp,"masternode",address,stake,"0","0","0","0","0","Masternode Payout","0",))
            conn.commit()






def masternodes_revalidate(c,m,app_log):
    "remove nodes that removed balance, to be run every 10k blocks"

    m.execute("SELECT * FROM masternodes")
    masternodes = m.fetchall()

    for masternode in masternodes:
        print (masternode)
        address = masternode[2]
        print ("address", address)
        balance_savings = masternode[3]
        print("balance_savings",balance_savings)
        balance = balanceget (address, c)[5]
        print ("balance", balance)
        if quantize_eight(balance) < 10000:
            m.execute("DELETE FROM masternodes WHERE address = ?",(address,))
            mas.commit()
        else: #update balance
            m.execute("UPDATE masternodes SET balance = ? WHERE address = ?",(balance,address))
            mas.commit ()
            app_log.warning("Masternode balance updated from {} to {} for {}".format(balance_savings,balance,address))




if __name__ == "__main__":


    import options
    config = options.Get ()
    config.read ()


    app_log = log.log ("solvency.log", "WARNING", True)
    mp.MEMPOOL = mp.Mempool (app_log,config,None,False)


    conn = sqlite3.connect('static/test.db')
    conn.text_factory = str
    c = conn.cursor()

    mas = sqlite3.connect("static/index_test.db")
    mas.text_factory = str
    m = mas.cursor()

    address = "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed"
    masternodes_update(c,m, "normal", 626580, app_log)
    masternodes_payout(c,m,70000, 1525304875, app_log)
    masternodes_revalidate (c, m, app_log)
