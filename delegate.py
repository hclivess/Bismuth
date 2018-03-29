import sqlite3
import time

conn = sqlite3.connect("static/ledger.db")
conn.text_factory = str
c = conn.cursor()

c.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
try:
    last_block = int(c.fetchone()[0])
except:
    last_block = 0

block_limit = last_block - 10000


def removals(database,cursor,escrow,block_limit):
    """cancel selected delegations and pay back, to be executed at an end of a phase"""
    cursor.execute("SELECT address, openfield FROM transactions WHERE recipient = ? AND block_height > ? AND openfield LIKE ?", (escrow, block_limit, "delegate:remove:" + '%',))
    removals = c.fetchall()
    print("removals",removals)

    for transaction in removals:
        address = transaction[0]
        signature = transaction[1].split(":")[2]
        print("address",address)
        print("signature", signature)

        try:
            # check if the source transaction exists
            cursor.execute("SELECT * FROM delegations WHERE recipient = ? AND signature = ? AND operation = ?", (escrow, signature,"add"))
            delegation_verified = (c.fetchall())[0]
            block_height = delegation_verified[0]

            try:
                amount = delegation_verified[4]
            except:
                amount = 0

            print ("delegations_verified", delegation_verified)

            #check if the payout already happened

            timestamp = int(time.time())



            try:
                cursor.execute("SELECT * FROM delegations WHERE address = ? AND recipient = ? AND operation = ? AND signature = ?", (escrow, address, "refund", signature,))
                dummy = c.fetchall()[0]
                print (dummy)
                print("already paid out in index")

            except:
                print("payout to index")
                c.execute("INSERT INTO delegations VALUES (?,?,?,?,?,?,?,?)", (block_height,timestamp, escrow, address,amount,signature,"refund","")) #index
                conn.commit()

                print("index payout finished")


                try:
                    cursor.execute("SELECT * FROM transactions WHERE address = ? AND recipient = ? AND openfield = ?", (escrow, address, "delegate:refund:" + signature,))
                    dummy = c.fetchall()[0]
                    print(dummy)
                    print("already paid out in ledger")

                except:
                    # payout if it didnt happen and index it
                    print("payout to ledger")

                    c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("0", timestamp, escrow, address, amount, "0", "0", "0", "0", "0", "0", "delegate:refund:" + signature))
                    conn.commit()

        except Exception as e:
            print(e, "not eligible for payout")



def additions(database,cursor,escrow,block_limit):
    """register all delegations to the index"""

    c.execute("CREATE TABLE IF NOT EXISTS delegations (block_height INTEGER, timestamp NUMERIC, address, recipient, amount NUMERIC, signature, operation, delegate)")

    delegates_list = []
    cursor.execute("SELECT DISTINCT openfield FROM transactions WHERE recipient = ? AND block_height > ? AND openfield LIKE ?", (escrow, block_limit, "delegate:add:" + '%',))
    additions = c.fetchall()

    for transaction in additions:
        delegates_list.append(transaction[0].split(":")[2])

    print ("delegates_list",delegates_list)

    for delegate in delegates_list:
        cursor.execute("SELECT block_height, timestamp, address, recipient, amount, signature, openfield FROM transactions WHERE address = ? AND recipient = ? AND block_height > ? AND openfield LIKE ?",(delegate,escrow,block_limit,"delegate:add:" + '%',))
        delegations = c.fetchall()

        #delegated_amount = 0
        for delegation in delegations:
            print("delegation",delegation)

            block_height = delegation[0]
            timestamp = delegation[1]
            address = delegation[2]
            recipient = delegation[3]
            amount = delegation[4]
            signature = delegation[5]
            operation = delegation[6].split(":")[1]
            delegate = delegation[6].split(":")[2]

            #delegate = delegation[2].split(":")[1]

            try:
                cursor.execute("SELECT * FROM delegations WHERE signature = ?",(signature,))
                dummy = c.fetchall()[0] #index only if it is not yet indexed
            except:
                cursor.execute("INSERT INTO delegations VALUES (?,?,?,?,?,?,?,?)",(block_height,timestamp, address, recipient, amount, signature, operation, delegate))
                conn.commit()

            #delegated_amount += amount
        #print ("total delegated",delegated_amount)

def list(database, cursor, block_limit):
    """determine top masternodes to know which are eligible for PoS rewards"""
    c.execute("SELECT DISTINCT delegate FROM delegations")
    delegates = c.fetchall()[0]
    print ("delegates",delegates)


    for delegate in delegates:
        c.execute("SELECT count(amount) FROM delegations WHERE delegate = ?", (delegate,))
        amounts = c.fetchall()[0][0]
        print(delegate, amounts)


additions(conn,c,"4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed",block_limit)
removals(conn,c,"4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed",block_limit)
list(conn,c,block_limit)