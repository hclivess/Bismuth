import sqlite3, keys

(key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read()

conn = sqlite3.connect('static/ledger.db')
conn.text_factory = str
c = conn.cursor()

c.execute('SELECT * FROM transactions WHERE recipient = ? AND openfield LIKE ? ORDER BY block_height DESC, timestamp DESC LIMIT 100;', (address,) + ('%' + "Archies:" + '%',))  # should work, needs testing
result_payouts = c.fetchall()

print result_payouts