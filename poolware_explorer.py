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



    output_addresses = []
    output_shares = []
    output_timestamps = []
    view = []
    view.append("Welcome to the opensource public Bismuth tool ran by the developer")
    view.append("<br>Config details in your config.txt:")
    view.append("<br>mining_ip=94.113.207.67")
    view.append("<br>mining_pool=1")
    view.append("<br>pool_address=4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed<br><br><br>")

    view.append('<head>\n')
    view.append('<link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.7/css/bootstrap.min.css" >')
    view.append('<script src="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.7/js/bootstrap.min.js"></script >')
    view.append('</head>\n')

    view.append('<body bgcolor = "white">\n')
    view.append("<body>")

    view.append("<table class='table table-responsive'>")
    view.append("<th>Address</th>")
    view.append("<th>Number of shares</th>")
    view.append("<th>First share registration date</th>")

    for x in set(addresses):
        s.execute("SELECT sum(shares) FROM shares WHERE address = ?", (x,))
        shares_sum = s.fetchone()[0]
        output_addresses.append(x)
        output_shares.append(shares_sum)

        s.execute("SELECT timestamp FROM shares WHERE address = ? ORDER BY timestamp ASC LIMIT 1", (x,))
        shares_timestamp = s.fetchone()[0]
        output_timestamps.append(shares_timestamp)

        color_cell = "white"


        view.append("<tr bgcolor ={}>".format(color_cell))
        view.append("<td>{}</td>".format(x))
        view.append("<td>{}</td>".format(shares_sum))
        view.append("<td>{}</td>".format(time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(float(shares_timestamp)))))
        view.append("<tr>")

    view.append("</table>")

    try:
        block_threshold = min(output_timestamps)
    except:
        block_threshold = time.time()

    print(address, block_threshold)

    view.append("<table class='table table-responsive'>")
    view.append("<th>Last blocks mined by this pool</th>")
    view.append("<tr>")
    for row in c.execute("SELECT block_height FROM transactions WHERE address = ? AND CAST(timestamp AS INTEGER) >= ? AND reward != 0", (address,)+(block_threshold,)):
        view.append("<td>{}</td>".format(row[0]))
        view.append("<tr>")
    view.append("</table>")


    view.append("</body>")
    return ''.join(view)

run(host='0.0.0.0', port=9080, debug=True)
hello()