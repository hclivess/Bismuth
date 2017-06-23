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

    c.execute("SELECT block_height,timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
    result = c.fetchall()
    last_block_height = result[0][0]
    last_timestamp = result[0][1]

    c.execute("SELECT * FROM transactions WHERE (openfield = ? OR openfield = ?) AND recipient = ? ORDER BY block_height DESC, timestamp DESC LIMIT 100;",("odd",)+("even",)+(address,))
    result_bets = c.fetchall()
    view_bets = []

    view_bets.append("<tr bgcolor=white>")
    view_bets.append("<td>Block Height</td><td>Time</td><td>Player</td><td>Block Hash</td><td>Hash Last Number</td><td>Amount Bet</td><td>Bet on</td><td>Result</td>")
    view_bets.append("</tr>")

    betting_signatures = []
    for x in result_bets:
        block_hash = x[7]
        openfield = str(x[11])
        betting_signatures.append(x[5]) #sig
        #print openfield
        digit_last = int((re.findall("(\d)", block_hash))[-1])
        if (digit_last % 2 == 0) and (openfield == "even"): #if bets even and wins
            cell_color = "#9b111e"
            result = "win"
        elif (digit_last % 2 != 0) and (openfield == "odd"): #if bets odd and wins
            cell_color = "#9b111e"
            result = "win"
        else:
            cell_color = "#49b675"
            result = "loss"

        view_bets.append("<tr bgcolor="+cell_color+">")
        view_bets.append("<td>{}</td>".format(x[0]))#block height
        view_bets.append("<td>{}".format(time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(float(x[1])))))#time
        view_bets.append("<td>{}</td>".format(x[2]))#player
        view_bets.append("<td>{}</td>".format(x[7]))#block hash
        view_bets.append("<td>{}</td>".format(digit_last))
        view_bets.append("<td>{}</td>".format(x[4]))
        view_bets.append("<td>{}</td>".format(x[11]))
        view_bets.append("<td>{}</td>".format(result))
        view_bets.append("</tr>")

    c.execute('SELECT * FROM transactions WHERE address = ? AND openfield LIKE ? ORDER BY block_height DESC, timestamp DESC LIMIT 100;',(address,)+('%'+"payout"+'%',)) #should work, needs testing
    result_payouts = c.fetchall()
    #print result_payouts
    view_payouts = []

    view_payouts.append("<tr bgcolor=white>")
    view_payouts.append("<td>Block Height</td><td>Time</td><td>Player</td><td>Block Hash</td><td>Amount</td>")
    view_payouts.append("</tr>")

    for x in result_payouts:
        #print betting_signatures
        if x[11].startswith("payout"):
            view_payouts.append("<tr bgcolor=lightblue>")
            view_payouts.append("<td>{}</td>".format(x[0])) #block height
            view_payouts.append("<td>{}</td>".format(time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(float(x[1]))))) #time
            view_payouts.append("<td>{}</td>".format(x[3]))  #player
            view_payouts.append("<td>{}</td>".format(x[7])) #block hash
            view_payouts.append("<td>{}</td>".format(x[4])) #amount
            view_payouts.append("</tr>")

    c.close()

    html = []
    html.append("<!DOCTYPE html>")
    html.append("<html>")
    html.append("<link rel = 'icon' href = 'static/zircodice.ico' type = 'image/x-icon' / >")
    html.append("<head>")
    html.append("<meta http-equiv='refresh' content='60' >")
    html.append("<link rel='stylesheet' type='text/css' href='static/style_zircodice.css'>")
    html.append("</head>")
    html.append("<META http-equiv='cache-control' content='no-cache'>")
    html.append("<TITLE>ZircoDice</TITLE>")
    html.append("<body><body background="'static/bg.jpg'"><center>")
    html.append("<h1>Welcome to ZircoDice</h1>")
    html.append("<p>Please send any amount of coins lower than 100 to the address <strong>"+address+"</strong> and include the word '<strong>even</strong>' or '<strong>odd</strong>' in the OpenField data.<br> You are betting on the last number in the block hash where your bet is included. 0 is considered an odd number.<br>If you win, you will receive 2x your bet. House returns 99% of your win minus fees. Payout happens after 10 blocks have passed.</p>")
    html.append("<br>")
    html.append("<h1>Bets</h1>")
    html.append("<table style='width:100%'>"+ str(''.join(view_bets))+"</table>")
    html.append("<h1>Payouts</h1>")
    html.append("<table style='width:100%'>" + str(''.join(view_payouts)) + "</table>")
    html.append("<p>We are currently at block {} from {} ({} minutes ago)</p>".format(last_block_height,time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(float(last_timestamp))),int((time.time() - float(last_timestamp))/60)))
    html.append("</body>")
    html.append("</html>")

    return str(''.join(html))

run(host='localhost', port=1212, debug=True)