import sqlite3, codecs, ast

conn = sqlite3.connect('static/ledger.db')
c = conn.cursor()

c.execute("select * from transactions where block_height = 205629")
result = c.fetchall()

#print (result)
try:
    result = (codecs.getdecoder("unicode_escape")(str(result))[0])
    #print (ast.literal_eval(str(result)))
except:
    print ("too bad")
#

#print (codexd.encode("utf-8").decode("utf-8"))

print (result) #CORRECT OUTPUT

"""
for x in result:
   print (x)
   for y in x:
       print (y)
"""


mempool = sqlite3.connect('mempool.db')
m = mempool.cursor()

for row in m.execute("select * from transactions"):
    result = row

    print (result)
    try:
        print (ast.literal_eval(str(result)))
        result = (codecs.getdecoder("unicode_escape")(str(result))[0])
    except:
        print ("too bad")