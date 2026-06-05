import sqlite3,time

reward_10 = 14.99999
row_init = 10
row_end = 660488
row = row_init
reward = reward_10

conn = sqlite3.connect('static/ledger.db')
conn.text_factory = str
c = conn.cursor()

c.execute("DELETE FROM transactions WHERE address = 'Development Reward'")
conn.commit()

while row <= row_end:
    print (row)
    timestamp = int(time.time())

    c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (0,timestamp,"Development Reward","4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed",reward,"0","0","0","0","0","0",row))

    row = row+10
    reward = reward - 0.00001
conn.commit()



