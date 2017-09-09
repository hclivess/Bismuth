import sqlite3, time, keys, options
from bottle import route, run, static_file

(key, private_key_readable, public_key_readable, public_key_hashed, address) = keys.read() #import keys

config = options.Get()
config.read()
debug_level = config.debug_level_conf
ledger_path_conf = config.ledger_path_conf
full_ledger = config.full_ledger_conf
ledger_path = config.ledger_path_conf
hyper_path = config.hyper_path_conf

@route('/static/<filename>')
def server_static(filename):
    return static_file(filename, root='static/')

@route('/')
def hello():

    # redraw chart
    if full_ledger == 1:
        conn = sqlite3.connect(ledger_path)
    else:
        conn = sqlite3.connect(hyper_path)
    c = conn.cursor()

    html = []
    for row in c.execute("SELECT * FROM transactions WHERE openfield LIKE ? ORDER BY block_height DESC LIMIT 500", ("html=" + '%',)):
        html.append("Block: ")
        html.append(str(row[0]))
        html.append("<br>")
        html.append("Time: ")
        html.append(time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(float(row[1]))))
        html.append("<br>")
        html.append("Author: ")
        html.append(str(row[2]))
        html.append("<br>")
        html.append("Content: ")
        html.append("<br><br>")
        html.append(row[11].lstrip("html="))
        html.append("<br><br>")
    return str(''.join(html))

run(host='0.0.0.0', port=4585, debug=True)