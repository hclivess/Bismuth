import sqlite3, hashlib

f = open('difficulty.log', 'w')
conn = sqlite3.connect('static/hyper.db')
c = conn.cursor()

c.execute("select * from transactions where reward != 0 and block_height != 0 order by block_height asc")
result = c.fetchall()



def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)

db_block_hash = "init"

for x in result:
    miner_address = x[2]
    nonce = x[11]

    diff_broke = 0
    diff = 0

    while diff_broke == 0:

        mining_hash = bin_convert(hashlib.sha224((miner_address + nonce + db_block_hash).encode("utf-8")).hexdigest())
        mining_condition = bin_convert(db_block_hash)[0:diff]
        if mining_condition in mining_hash:
            diff_result = diff
            diff = diff + 1
        else:
            diff_broke = 1
    try:


        f.write(str(x[0]) + " " + str(diff_result) + "\n")
        print (x[0],diff_result)

    except:
        pass

    db_block_hash = x[7] #current for next run as previous

f.close()