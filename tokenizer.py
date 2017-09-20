import sqlite3, re

conn = sqlite3.connect('static/hyper.db')
conn.text_factory = str
c = conn.cursor()

#issue token: token:issue:worthless:10000
#transfer token: token:transfer:worthless:10:f9975bfc90c90f46768d4165b8201830605402a9ebe759f601677e6d

#print all token issuances
c.execute("SELECT block_height, address, openfield FROM transactions WHERE openfield LIKE ? ORDER BY block_height ASC;", ("token:issue" + '%',))
results = c.fetchall()
print (results)

for x in results:
    token = re.findall("token\:issue\:(.*)\:[\d]+",x[2])[0]
    print(token)

    issued_by = x[1]
    print (issued_by)

    amounts = re.findall("token\:issue\:.*\:([\d]+)",x[2])[0]
    print(amounts)
#print all token issuances

#print all token transfers
c.execute("SELECT block_height, address, openfield FROM transactions WHERE openfield LIKE ? ORDER BY block_height ASC;", ("token:transfer" + '%',))
results = c.fetchall()
print (results)

for x in results:
    token = re.findall("token\:transfer\:(.*)\:[\d]+",x[2])[0]
    print(token)

    transer_from = x[1]
    print (transer_from)

    transfer_to = re.findall("token\:transfer\:.*\:[\d]+:(.*)", x[2])[0]
    print (transfer_to)

    transfer_amount = re.findall("token\:transfer\:.*\:([\d]+)",x[2])[0]
    print (transfer_amount)

#print all token transfers


conn.close()