import sqlite3, time, re, keys
from bottle import route, run, static_file

(key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read() #import keys

@route('/static/<filename>')
def server_static(filename):
    return static_file(filename, root='static/')

@route('/')
def hello():

    # redraw chart
    conn = sqlite3.connect('static/ledger.db')
    c = conn.cursor()

    html = []
    for row in c.execute("SELECT * FROM transactions WHERE openfield LIKE ? LIMIT 500", ("html=" + '%',)):
        html.append(row[11].lstrip("html="))

    return str(''.join(html))

run(host='0.0.0.0', port=4585, debug=True)