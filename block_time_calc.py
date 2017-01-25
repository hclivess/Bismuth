import sqlite3
import os

conn = sqlite3.connect("static/ledger.db")  # open to select the last tx to create a new hash from
conn.text_factory = str
c = conn.cursor()
c.execute("SELECT timestamp,block_height FROM transactions WHERE reward = 10 and block_height >= 20000")
result = c.fetchall()
#print result

l = []
y = 0
for x in result:
    #print x[0]
    #print y
    ts_difference = float(x[0]) - float(y)
    print str(x[1])+" "+str(ts_difference)
    l.append(ts_difference)
    y = x[0]