import time
import sqlite3

depth = 10000

conn = sqlite3.connect('ledger.db')
conn.text_factory = str
c = conn.cursor()

end_balance = 0
addresses = []

c.execute("SELECT block_height FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
db_block_height = c.fetchone()[0]
print db_block_height


for row in c.execute("SELECT * FROM transactions WHERE block_height < ? ORDER BY block_height;",(str(int(db_block_height) - depth),)):
    db_address = row[2]
    db_recipient = row[3]
    addresses.append (db_address)
    addresses.append (db_recipient)

unique_addressess = set(addresses)

for x in set(unique_addressess):
    if x != "genesis":

        c.execute("SELECT sum(amount) FROM transactions WHERE recipient = ?;",(x,))
        credit = c.fetchone()[0]
        if credit == None:
            credit = 0

        c.execute("SELECT sum(amount) FROM transactions WHERE address = ?;",(x,))
        debit = c.fetchone()[0]
        if debit == None:
            debit = 0

        c.execute("SELECT sum(fee) FROM transactions WHERE address = ?;",(x,))
        fees = c.fetchone()[0]
        if fees == None:
            fees = 0

        c.execute("SELECT sum(reward) FROM transactions WHERE address = ?;",(x,))
        rewards = c.fetchone()[0]
        if rewards == None:
            rewards = 0

        end_balance = credit - debit - fees + rewards
        print x
        print end_balance

        if end_balance > 0:
            timestamp = str(time.time())
            c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("0",timestamp,"Hyperblock",x,str(float(end_balance)),"0","0","0","0","0","0","0"))
            conn.commit()

c.execute("DELETE FROM transactions WHERE block_height < ? AND address != 'Hyperblock';",(str(int(db_block_height) - depth),))
conn.commit()

c.execute("VACUUM")
conn.close()