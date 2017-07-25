import sqlite3, codecs

conn = sqlite3.connect('static/ledger.db')
c = conn.cursor()

c.execute("select * from transactions where block_height = 205629")
result = c.fetchall()

codexd = (codecs.getdecoder("unicode_escape")(str(result))[0])

print (codexd.encode("utf-8").decode("utf-8"))


for x in result:
   print (x)
   for y in x:
       print (y)
