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
    removals_list = []
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
            cursor.execute("SELECT amount, openfield FROM transactions WHERE recipient = ? AND block_height > ? AND signature = ? AND openfield LIKE ?", (escrow, block_limit, signature, "delegate:add:" + '%',))
            delegation_verified = (c.fetchall())[0]

            try:
                amount = delegation_verified[0]
            except:
                amount = 0

            print ("delegations_verified",delegation_verified)

            #check if the payout already happened
            try:
                cursor.execute("SELECT * FROM transactions WHERE address = ? AND recipient = ? AND openfield = ?", (escrow, address, "delegate:refund:"+signature,))
                dummy = c.fetchall()[0]
                print (dummy)
                print("already paid out")
            except:
                #payout if it didnt happen and index it
                print("payout")
                timestamp = int(time.time())

                c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("0",timestamp, escrow, address,amount,"0","0","0","0","0","0","delegate:refund:" +signature))
                conn.commit()


                print("payout finished")
        except Exception as e:
            print(e, "not eligible for payout")



def additions(database,cursor,escrow,block_limit):
    delegates_list = []
    cursor.execute("SELECT DISTINCT openfield FROM transactions WHERE recipient = ? AND block_height > ? AND openfield LIKE ?", (escrow, block_limit, "delegate:add:" + '%',))
    additions = c.fetchall()

    for transaction in additions:
        delegates_list.append(transaction[0].split(":")[2])

    print ("delegates_list",delegates_list)

    for delegate in delegates_list:
        cursor.execute("SELECT amount, signature, openfield FROM transactions WHERE address = ? AND recipient = ? AND block_height > ? AND openfield LIKE ?",(delegate,escrow,block_limit,"delegate:add:" + '%',))
        delegations = c.fetchall()

        delegated_amount = 0
        for delegation in delegations:
            print("delegation",delegation)
            #address = delegation[0]

            amount = delegation[0]
            txid = delegation[1][:56]

            print("amount", amount)
            print("txid", txid)
            #delegate = delegation[2].split(":")[1]

            delegated_amount += amount
        print ("total delegated",delegated_amount)


additions(conn,c,"4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed",block_limit)
removals(conn,c,"4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed",block_limit)

