import math
import sqlite3

conn = sqlite3.connect('ledger.db')
conn.text_factory = str
c = conn.cursor()

c.execute("SELECT distinct block_height FROM transactions where reward = 10")
all_blocks = c.fetchall()
print all_blocks

diffs_all = []

for x in all_blocks:
    try:
        # calculate difficulty
        c.execute("SELECT timestamp FROM transactions WHERE block_height = '" + str(x[0]) + "'")
        timestamp_last_block = float(c.fetchall()[-1][0])  # select the reward block
        #print timestamp_last_block

        c.execute("SELECT avg(timestamp) FROM transactions where block_height >= '" + str(x[0] - 30) + "' and reward = 10 and block_height <= '" + str(x[0]) + "'")
        timestamp_avg = c.fetchall()[0][0]  # select the reward block
        #print timestamp_avg

        timestamp_difference = timestamp_last_block - timestamp_avg
        #print timestamp_difference

        diff = (math.log(1e18/timestamp_difference))

        print diff

        diffs_all.append(diff)
    except Exception, e:
        print "problem "+str(e)+" with block diff calc at: "+str(x[0])
        pass

print diffs_all