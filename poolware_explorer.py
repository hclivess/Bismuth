import sqlite3, time
from bottle import route, run, static_file

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
    view = []
    for x in set(addresses):
        s.execute("SELECT sum(shares) FROM shares WHERE address = ?", (x,))
        shares_sum = s.fetchone()[0]
        output_addresses.append(x)
        output_shares.append(shares_sum)

        color_cell = "white"
        view.append("<table class='table table-responsive'>")
        view.append("<tr bgcolor ={}>".format(color_cell))
        view.append("<th>{}</td>".format(x))
        view.append("<th>{}</td>".format(shares_sum))
        view.append("<tr>")



    return ''.join(view)

run(host='0.0.0.0', port=9080, debug=True)
hello()