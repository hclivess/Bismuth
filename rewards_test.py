import sqlite3

conn = sqlite3.connect('static/ledger.db')
conn.text_factory = str
c = conn.cursor()

old_row = 10
for row in c.execute('select * from transactions where recipient = "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed" and block_height = 0 and openfield != "salvage" order by CAST(openfield AS INTEGER) asc'):
    if int(row[11]) != old_row:
        print ("error at",old_row, row)
    old_row = int(row[11]) + 10