import sqlite3,math,time

def get_diff():
    conn = sqlite3.connect('static/ledger.db')
    conn.text_factory = str
    c = conn.cursor()

    c.execute("SELECT block_height,timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1")
    result = c.fetchall()
    db_block_height = result[0][0]
    db_timestamp_last = float(result[0][1])
                    
    c.execute("SELECT avg(timestamp) FROM transactions where block_height >= ? and reward != 0;", (str(db_block_height - 30),))
    timestamp_avg = c.fetchall()[0][0]  # select the reward block
    timestamp_difference = float(db_timestamp_last) - timestamp_avg
    diff = (math.log(1e20 / timestamp_difference))

    # retarget
    c.execute("SELECT block_height FROM transactions WHERE CAST(timestamp AS INTEGER) > ? AND reward != 0",(db_timestamp_last - 600,)) #600=10 min
    blocks_per_minute = len(c.fetchall())/10 #/10=1 min
    conn.close()

    if blocks_per_minute > 1:  # if more blocks than 1 per minute
        diff = diff + blocks_per_minute

    # drop diff per minute if over target
    time_drop = time.time()
    if time_drop > db_timestamp_last + 180: #start dropping after 3 minutes
        diff = diff - (time_drop - db_timestamp_last) / 60 #drop 1 diff per minute
    # drop diff per minute if over target
    if diff < 35:
        diff = 35
    # retarget

    return diff

print get_diff()
