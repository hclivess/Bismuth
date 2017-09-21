import sqlite3

conn = sqlite3.connect('static/hyper.db')
conn.text_factory = str
c = conn.cursor()

#issue token: token:issue:worthless:10000
#transfer token: token:transfer:worthless:10:f9975bfc90c90f46768d4165b8201830605402a9ebe759f601677e6d

#print all token issuances
c.execute("SELECT block_height, address, openfield FROM transactions WHERE openfield LIKE ? ORDER BY block_height ASC;", ("token:issue" + '%',))
results = c.fetchall()
#print (results)

for x in results:
    token = x[2].split(":")[2]
    #print("token", token)

    issued_by = x[1]
    #print ("issued_by", issued_by)

    total = x[2].split(":")[3]
    #print("total", total)
#print all token issuances


#print all transfers of a given token
token = "worthless"
c.execute("SELECT block_height, address, openfield FROM transactions WHERE openfield LIKE ? ORDER BY block_height ASC;", ("token:transfer:" + token + ':%' ,))
r = c.fetchall()[0]
print (r)


#address_from = total # init
#address_to = 0 # init

token = r[2].split(":")[2]
print("token", token)

transer_from = results[1]
print ("transfer_from", transer_from)

transfer_to = r[2].split(":")[4]
print ("transfer_to", transfer_to)

transfer_amount = r[2].split(":")[3]
print ("transfer_amount",transfer_amount)

#print all token transfers


conn.close()