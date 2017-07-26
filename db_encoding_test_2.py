import sqlite3, pickle

conn = sqlite3.connect('static/ledger.db')
c = conn.cursor()

c.execute("select * from transactions where block_height = 205629")
result = pickle.dumps(c.fetchall())

output = pickle.loads(result)
print(output)

for x in output:
    print(x)
    for y in x:
        print(y)