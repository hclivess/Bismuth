"""One-off sanity check for the 'Development Reward' rows (attic).

A retired read-only audit script: it walks the ``Development Reward``
transactions in ``static/ledger.db`` in block order and prints any place where
the openfield (block-interval) counter does not advance by exactly 10, i.e. a
gap left by ``rewards_reindex.py``. Run manually; not part of the node or tests.
"""

import sqlite3

conn = sqlite3.connect('static/ledger.db')
conn.text_factory = str
c = conn.cursor()

old_row = 10
for row in c.execute('select * from transactions where recipient = "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed" and block_height = 0 and operation = "Development Reward" order by CAST(openfield AS INTEGER) asc'):
    if int(row[11]) != old_row:
        print ("error at",old_row, row)
    old_row = int(row[11]) + 10