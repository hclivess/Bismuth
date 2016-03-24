import sqlite3

last_block = 3
conn = sqlite3.connect('ledger.db')
c = conn.cursor()
c.execute("DELETE FROM transactions WHERE block_height > '"+str(last_block)+"' ")
conn.commit() # Save (commit) the changes
conn.close()
