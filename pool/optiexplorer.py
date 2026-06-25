# optiexplorer.py v 0.32 to be used with Python3.5 or better
# Copyright Hclivess, Maccaspacca, vv181 2017
# Copyright Maccaspacca 2018
# for license see LICENSE file
# ..

from tornado.wsgi import WSGIContainer
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop

import sqlite3, time, essentials
from flask import Flask, render_template
app = Flask(__name__)

key, public_key_readable, private_key_readable, _, _, public_key_hashed, address = essentials.keys_load ("privkey.der", "pubkey.der")

# load config

try:

    lines = [line.rstrip('\n') for line in open('pool.txt')]
    for line in lines:
        try:
            if "m_timeout=" in line:
                m_timeout = int(line.split('=')[1])
        except Exception as e:
            m_timeout = 5

except Exception as e:
    m_timeout = 5

# load config

#@app.route('/static/<filename>')
# def server_static(filename):
# return static_file(filename, root='static/')


@app.route('/')
# def hello():
def main():

    conn = sqlite3.connect('static/ledger.db')
    conn.text_factory = str
    c = conn.cursor()

    shares = sqlite3.connect('shares.db')
    shares.text_factory = str
    s = shares.cursor()

    oldies = sqlite3.connect('archive.db')
    oldies.text_factory = str
    o = oldies.cursor()

    addresses = []
    for row in s.execute("SELECT * FROM shares"):
        shares_address = row[0]
        shares_value = row[1]
        shares_timestamp = row[2]

        if shares_address not in addresses:
            addresses.append(shares_address)

    total_hash = 0
    worker_count = 0
    output_shares = []
    output_timestamps = []

    data_addres = []
    data_shares = []
    data_mrate = []
    data_mname = []
    data_wcount = []

    for x in addresses:
        s.execute(
            "SELECT sum(shares) FROM shares WHERE address = ? AND paid != 1", (x,))
        shares_sum = s.fetchone()[0]
        if shares_sum == None:
            shares_sum = 0
            continue
        output_shares.append(shares_sum)

        s.execute(
            "SELECT timestamp FROM shares WHERE address = ? ORDER BY timestamp ASC LIMIT 1", (x,))
        shares_timestamp = s.fetchone()[0]
        output_timestamps.append(float(shares_timestamp))

        s.execute(
            "SELECT * FROM shares WHERE address = ? ORDER BY timestamp DESC LIMIT 1", (x,))
        shares_last = s.fetchone()
        #mrate = shares_last[4]
        mname = shares_last[7]  # last worker

        s.execute("SELECT DISTINCT name FROM shares WHERE address = ?", (x,))
        shares_names = s.fetchall()

        nrate = []
        ncount = []
        for n in shares_names:
            s.execute(
                "SELECT * FROM shares WHERE address = ? AND name = ? ORDER BY timestamp DESC LIMIT 1", (x, n[0]))
            names_last = s.fetchone()
            t1 = time.time()
            t2 = float(names_last[2])
            t3 = (t1 - t2) / 60
            if t3 < m_timeout:
                nrate.append(int(names_last[4]))
                ncount.append(int(names_last[6]))
            else:
                nrate.append(0)
                ncount.append(0)

        mrate = sum(nrate)  # hashrate of address
        wcount = sum(ncount)  # worker count
        total_hash = total_hash + mrate
        worker_count = worker_count + wcount

        if t3 < 30:

            data_addres.append(x)
            data_shares.append(shares_sum)
            data_mrate.append(str(mrate))
            data_mname.append(mname)
            data_wcount.append(str(wcount))

    try:
        shares_total = sum(output_shares)
    except:
        shares_total = 0

    try:
        block_threshold = min(output_timestamps)
    except:
        block_threshold = time.time()

    reward_list = []
    data_block = []
    data_reward = []
    data_tShares = []
    data_rewardps = []
    data_tReward = []
    data_tHash = []
    data_twcount = []

    for row in c.execute("SELECT * FROM transactions WHERE address = ? AND CAST(timestamp AS INTEGER) >= ? AND reward != 0", (address,) + (block_threshold,)):
        data_block.append(row[0])
        data_reward.append(row[9])
        reward_list.append(float(row[9]))

    if data_block == [] and data_reward == []:
        data_block.append(0)
        data_reward.append(0)

    reward_total = sum(reward_list)

    try:
        reward_per_share = reward_total / shares_total
    except:
        reward_per_share = 0

    data_tShares.append(shares_total)
    data_rewardps.append(reward_per_share)
    data_tReward.append(reward_total)
    data_tHash.append(format('%.2f' % (total_hash / 1000)))
    data_twcount.append(worker_count)

    # payout view
    data_pendingaddress = []
    data_pendingreward = []
    if reward_total > 0:

        for x, y in zip(addresses, output_shares):

            try:
                claim = y * reward_per_share
            except:
                claim = 0

            data_pendingaddress.append(x)
            data_pendingreward.append(format('%.8f' % (claim)))

    data_paddress = []
    data_bismuthreward = []
    data_blockheight = []
    data_ptime = []

    for row in c.execute("SELECT * FROM transactions WHERE address = ? and openfield = ? ORDER BY timestamp DESC LIMIT 80", (address,) + ("pool",)):
        data_paddress.append(row[3])
        data_bismuthreward.append(row[4])
        data_blockheight.append(row[0])
        data_ptime.append(format(time.strftime(
            "%Y/%m/%d,%H:%M:%S", time.gmtime(float(row[1])))))

    conn.close()
    shares.close()
    oldies.close()

    return render_template("index.html",
                           recentminers=zip(
                               data_addres, data_shares, data_mrate, data_mname, data_wcount),
                           bpstats=zip(data_block, data_reward, data_tShares,
                                       data_rewardps, data_tReward, data_tHash, data_twcount),
                           payouts=zip(data_addres, data_bismuthreward,
                                       data_blockheight, data_ptime),
                           payoutsfees=zip(data_pendingaddress,
                                           data_pendingreward)
                           )


if __name__ == "__main__":
    #app.run(host='0.0.0.0', port=9080, debug=True)
	http_server = HTTPServer(WSGIContainer(app))
	http_server.listen(9080)
	IOLoop.instance().start()
