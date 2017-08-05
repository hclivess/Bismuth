import sqlite3, time, keys
from bottle import route, run, static_file

(key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read() #import keys

@route('/static/<filename>')
def server_static(filename):
    return static_file(filename, root='static/')

@route('/')
def hello():

    conn = sqlite3.connect('static/ledger.db')
    conn.text_factory = str
    c = conn.cursor()

    shares = sqlite3.connect('shares.db')
    shares.text_factory = str
    s = shares.cursor()

    addresses = []
    for row in s.execute("SELECT * FROM shares"):
        shares_address = row[0]
        shares_value = row[1]
        shares_timestamp = row[2]

        if shares_address not in addresses:
            addresses.append(shares_address)

    output_shares = []
    output_timestamps = []
    view = []

    view.append("Welcome to the opensource public Bismuth pool ran by the developer")
    view.append("<br>Config details in your config.txt:")
    view.append("<br>pool_ip=94.113.207.67")
    view.append("<br>mining_pool=1")
    view.append("<br>pool_address={}<br><br><br>".format(address))

    view.append('<head>\n')
    view.append('<link rel = "icon" href = "static/explorer.ico" type = "image/x-icon" />')
    view.append('<link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.7/css/bootstrap.min.css" >')
    view.append('<script src="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.7/js/bootstrap.min.js"></script >')
    view.append('<title>Bismuth Pool</title>')
    view.append('</head>\n')

    view.append('<body bgcolor = "white">\n')
    view.append("<body>")

    view.append("<h3>Share statistics</h3>")
    view.append("<table class='table table-responsive'>")
    view.append("<th>Address</th>")
    view.append("<th>Number of shares</th>")
    view.append("<th>First share registration date</th>")

    for x in addresses:
        s.execute("SELECT sum(shares) FROM shares WHERE address = ? AND paid != 1", (x,))
        shares_sum = s.fetchone()[0]
        if shares_sum == None:
            shares_sum = 0
        output_shares.append(shares_sum)


        s.execute("SELECT timestamp FROM shares WHERE address = ? ORDER BY timestamp ASC LIMIT 1", (x,))
        shares_timestamp = s.fetchone()[0]
        output_timestamps.append(float(shares_timestamp))

        color_cell = "white"


        view.append("<tr bgcolor ={}>".format(color_cell))
        view.append("<td>{}</td>".format(x))
        view.append("<td>{}</td>".format(shares_sum))
        view.append("<td>{}</td>".format(time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(float(shares_timestamp)))))
        view.append("<tr>")

    try:
        shares_total = sum(output_shares)
    except:
        shares_total = 0



    view.append("</table>")

    try:
        block_threshold = min(output_timestamps)
    except:
        block_threshold = time.time()

    view.append("<table class='table table-responsive'>")
    reward_list = []
    for row in c.execute("SELECT * FROM transactions WHERE address = ? AND CAST(timestamp AS INTEGER) >= ? AND reward != 0", (address,)+(block_threshold,)):
        view.append("<td>{}</td>".format(row[0]))
        view.append("<td>{}</td>".format(row[9]))
        view.append("<tr>")
        reward_list.append(float(row[9]))

    view.append("<th>Shares total</th>")
    view.append("<td>{}</td>".format(shares_total))
    view.append("<tr>")

    reward_total = sum(reward_list)

    view.append("<th>Reward per share</th>")
    try:
        reward_per_share = reward_total/shares_total
    except:
        reward_per_share = 0

    view.append("<td>{}</td>".format(reward_per_share))
    view.append("<tr>")

    view.append("<th>Mined rewards for this round</th>")
    view.append("<td>{}</td>".format(reward_total))
    view.append("<tr>")

    view.append("</table>")

    # payout view
    view.append("<h3>Pending payouts</h3>")
    view.append("<table class='table table-responsive'>")
    view.append("<th>Address</th>")
    view.append("<th>Bismuth reward</th>")
    view.append("<tr>")


    for x, y in zip(addresses, output_shares):

        try:
            claim = y*reward_per_share
        except:
            claim = 0

        view.append("<td>{}</td>".format(x))
        view.append("<td>{}</td>".format('%.8f' %(claim)))
        view.append("<tr>")
    # payout view
    view.append("</table>")

    # history view
    view.append("<table class='table table-responsive'>")
    view.append("<h3>Previous payouts</h3>")
    view.append("<th>Address</th>")
    view.append("<th>Bismuth reward</th>")
    view.append("<th>Block height</th>")
    view.append("<th>Time</th>")
    view.append("<tr>")
    for row in c.execute("SELECT * FROM transactions WHERE address = ? and openfield = ?",(address,)+("pool",)):
        view.append("<td>{}</td>".format(row[3]))
        view.append("<td>{}</td>".format(row[4]))
        view.append("<td>{}</td>".format(row[0]))
        view.append("<td>{}</td>".format(time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(float(row[1])))))
        view.append("<tr>")

    # history view

    view.append("</body>")

    conn.close()
    shares.close()

    return ''.join(view)

run(host='0.0.0.0', port=9080, debug=True)