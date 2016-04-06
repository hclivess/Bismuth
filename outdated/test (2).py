from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto import Random
import sqlite3

#open dbs for mempool backup and followup deletion
conn = sqlite3.connect('ledger.db')
c = conn.cursor()
c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
txhash_db = c.fetchone()[0]

#backup all followups to mempool
mempool = sqlite3.connect('mempool.db')
m = mempool.cursor()

for row in c.execute('SELECT * FROM transactions WHERE block_height > "'+str(1)+'"'): #move all blocks higher than 1
    db_block_height = row[0]
    db_timestamp = row[1]
    db_address = row[2]
    db_to_address = row[3]
    db_amount = row [4]
    db_signature = row[5]
    db_public_key_readable = row[6]
    db_public_key = RSA.importKey(row[6])
    db_txhash = row[7]
    db_transaction = str(db_timestamp) +":"+ str(db_address) +":"+ str(db_to_address) +":"+ str(db_amount) 

    m.execute("INSERT INTO transactions VALUES ('"+str(db_block_height)+"','"+str(db_timestamp)+"','"+str(db_address)+"','"+str(db_to_address)+"','"+str(db_amount)+"','"+str(db_signature)+"','"+str(db_public_key_readable)+"','"+str(db_txhash)+"')") # Insert a row of data
mempool.commit()
mempool.close()
conn.close()
#backup all followups to mempool
