import sqlite3, base64, hashlib

public_key_readable = open('pubkey.der').read()
public_key_hashed = base64.b64encode(public_key_readable.encode("utf-8"))
address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()

mempool = sqlite3.connect('mempool.db')
mempool.text_factory = str
m = mempool.cursor()
m.execute("SELECT sum(amount) FROM transactions WHERE address = ?;", (address,))
debit_mempool = m.fetchone()[0]
mempool.close()
if debit_mempool == None:
    debit_mempool = 0

conn = sqlite3.connect('static/ledger.db')
conn.text_factory = str
c = conn.cursor()
c.execute("SELECT sum(amount) FROM transactions WHERE recipient = ?;", (address,))
credit = c.fetchone()[0]
c.execute("SELECT sum(amount) FROM transactions WHERE address = ?;", (address,))
debit = c.fetchone()[0]
c.execute("SELECT sum(fee) FROM transactions WHERE address = ?;", (address,))
fees = c.fetchone()[0]
c.execute("SELECT sum(reward) FROM transactions WHERE address = ?;", (address,))
rewards = c.fetchone()[0]
c.execute("SELECT MAX(block_height) FROM transactions")
bl_height = c.fetchone()[0]

if debit == None:
    debit = 0
if fees == None:
    fees = 0
if rewards == None:
    rewards = 0
if credit == None:
    credit = 0
balance = credit - debit - fees + rewards - debit_mempool

print("Public key address: {}".format(address))
print("Total fees paid: {}".format(fees))
print("Total rewards mined: {}".format(rewards))
print("Total tokens received: {}".format(credit))
print("Total tokens spent: {}".format(debit))
print("Transction address balance: {}".format(balance))