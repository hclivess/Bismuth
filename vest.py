import sqlite3
import log
from decimal import *

def delegates_update(file, ledger, mode, app_log):
    if mode not in ("normal","reindex"):
        raise ValueError ("Wrong value for delegates_update function")

    conn = sqlite3.connect(ledger)
    conn.text_factory = str
    c = conn.cursor()

    deleg = sqlite3.connect(file)
    deleg.text_factory = str
    d = deleg.cursor()
    d.execute("CREATE TABLE IF NOT EXISTS delegates (block_height INTEGER, timestamp, address, recipient, txid, amount INTEGER)")
    deleg.commit()

    if mode == "reindex":
        app_log.warning("Delegate database will be reindexed")
        d.execute("DELETE FROM delegates")
        deleg.commit()


    d.execute("SELECT block_height FROM delegates ORDER BY block_height DESC LIMIT 1;")
    try:
        delegate_last_block = int(d.fetchone()[0])
    except:
        delegate_last_block = 0

    app_log.warning("Delegate anchor block: {}".format(delegate_last_block))

    c.execute("SELECT block_height, timestamp, address, recipient, signature, openfield FROM transactions WHERE block_height >= ? AND openfield LIKE ? AND reward = 0 ORDER BY block_height ASC;", (delegate_last_block, "vest:" + '%',))
    result = c.fetchall()


    for delegation in result:

        block_height = delegation[0]
        timestamp = delegation[1]
        sender = delegation[2]
        recipient = delegation[3]
        txid = delegation[4][:56]

        try:
            operation = str(delegation[5].split(":")[1])
            vest = int(delegation[5].split(":")[2])
        except:
            vest = 0 #todo: exit loop here on invalid vest
            operation = None

        print("operation",operation)

        if operation == "add":
            #vested_out = 0
            d.execute("SELECT sum(amount) FROM delegates WHERE address = ?", (sender,))
            try:
                vested_out = int(d.fetchone()[0])
                print ("vested_out",vested_out)
            except:
                vested_out = 0


            # balance
            c.execute("SELECT sum(amount)+sum(reward) FROM transactions WHERE recipient = ? ", (sender,))
            try:
                credit = Decimal(c.fetchone()[0]).quantize(Decimal('0.00000000'))
            except Exception as e:
                credit = 0
            c.execute("SELECT sum(amount)+sum(fee) FROM transactions WHERE address = ? ", (sender,))
            try:
                debit = Decimal(c.fetchone()[0]).quantize(Decimal('0.00000000'))
            except Exception as e:
                debit = 0
            balance = credit - debit
            # balance

            print(balance)

            if vest > 0:
                vested_out = vested_out + int(vest)

            if vested_out > balance:
                vested_out = 0

            print (vested_out)

            try:
                d.execute("SELECT * from delegates WHERE txid = ?", (txid,))
                dummy = d.fetchall()[0]  # check for uniqueness
                app_log.warning("Delegation operation already processed: {}".format(txid))
            except:
                if vested_out > 0:
                    d.execute("INSERT INTO delegates VALUES (?,?,?,?,?,?)", (block_height, timestamp, sender, recipient, txid, vested_out))
                    deleg.commit()

        if operation == "remove":
            try:
                remove_txid = str(delegation[5].split(":")[2])

                d.execute("DELETE FROM delegates WHERE txid = ?",(remove_txid,))
                deleg.commit()
                app_log.warning("Removed delegation if found: {}".format(remove_txid))

            except:
                app_log.warning("Unable to remove delegation: {}".format(remove_txid))

        if operation == "cleanse":
            try:
                d.execute("DELETE FROM delegates WHERE address = ?",(sender,))
                deleg.commit()
                app_log.warning("Removed all delegations if found")
            except:
                app_log.warning("Unable to remove delegations")



if __name__ == "__main__":
    app_log = log.log("delegates.log", "WARNING", True)
    delegates_update("static/index.db","static/ledger.db","normal",app_log)
















