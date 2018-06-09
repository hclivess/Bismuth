import sqlite3, time, re, keys, options
from decimal import *
import essentials

import tornado.ioloop
import tornado.web
from random import randint

key, public_key_readable, private_key_readable, _, _, public_key_hashed, address = essentials.keys_load_new("wallet.der")

config = options.Get()
config.read()
debug_level = config.debug_level_conf
ledger_path_conf = config.ledger_path_conf
full_ledger = config.full_ledger_conf
ledger_path = config.ledger_path_conf
hyper_path = config.hyper_path_conf

block_anchor = 547989

print("Mounting roll database...")
roll_db = sqlite3.connect("roll.db")
roll_db.text_factory = str
roll_cursor = roll_db.cursor()
print("Roll database mounted...")

def percentage_of(part, whole):
    getcontext().prec = 2  # decimal places
    try:
        result = 100 * (Decimal(part) / Decimal(whole))
    except:
        result = 0

    return '%.2f' % result


def probability_count(roll_cursor):
    roll_cursor.execute("SELECT COUNT(*) FROM transactions WHERE rolled IN (?,?,?,?,?)", (0,) + (2,) + (4,) + (6,) + (8,))
    sum_even = roll_cursor.fetchone()[0]
    roll_cursor.execute("SELECT COUNT(*) FROM transactions WHERE rolled IN (?,?,?,?,?)", (1,) + (3,) + (5,) + (7,) + (9,))
    sum_odd = roll_cursor.fetchone()[0]

    return sum_even, sum_odd



def balancesimple(cursor_db,address):

    cursor_db.execute("SELECT sum(amount) FROM transactions WHERE recipient = ?;", (address,))
    credit_ledger = cursor_db.fetchone()[0]
    credit_ledger = 0 if credit_ledger is None else float('%.8f' % credit_ledger)
    credit = float(credit_ledger)

    cursor_db.execute("SELECT sum(fee),sum(reward),sum(amount) FROM transactions WHERE address = ?;", (address,))
    result = cursor_db.fetchall()[0]

    fees = result[0]
    fees = 0 if fees is None else float('%.8f' % fees)

    rewards = result[1]
    rewards = 0 if rewards is None else float('%.8f' % rewards)

    debit_ledger = result[2]
    debit_ledger = 0 if debit_ledger is None else float('%.8f' % debit_ledger)

    balance = float('%.8f' % (float(credit) - float(debit_ledger) - float(fees) + float(rewards)))

    return balance


def roll(block_height, txid, roll_db, roll_cursor):
    roll_cursor.execute("CREATE TABLE IF NOT EXISTS transactions (block_height INTEGER, txid, rolled)")
    roll_db.commit()

    try:
        roll_cursor.execute("SELECT rolled FROM transactions WHERE txid = ?",(txid,))
        roll_number = roll_cursor.fetchone()[0]
    except:
        roll_number = (randint(0, 9))
        roll_cursor.execute("INSERT INTO transactions VALUES (?,?,?)",(block_height, txid, roll_number))

    roll_db.commit()
    return roll_number

class MainHandler(tornado.web.RequestHandler):
    def get(self):
        print("Main object engaged")

        # redraw chart


        while True:
            try:
                print("Mounting database...")
                if full_ledger == 1:
                    conn = sqlite3.connect(ledger_path)
                else:
                    conn = sqlite3.connect(hyper_path)
                c = conn.cursor()
                print("Database mounted...")

                c.execute("SELECT block_height, timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
                result = c.fetchall()
                last_block_height = result[0][0]
                last_timestamp = result[0][1]

                c.execute("SELECT * FROM transactions WHERE (openfield = ? OR openfield = ?) AND recipient = ? AND block_height > ? ORDER BY block_height DESC, timestamp DESC LIMIT 1000;",("odd",)+("even",)+(address,)+(block_anchor,))
                result_bets = c.fetchall()

                break

            except Exception as e:
                print("Retrying database access, {}".format(e))
                time.sleep(1)

        view_bets = []
        view_bets.append("<tr bgcolor=white>")
        view_bets.append("<td>Block Height</td><td>Time</td><td>Player</td><td>TXID</td><td>Casino Roll</td><td>Amount Bet</td><td>Bet on</td><td>Result</td>")
        view_bets.append("</tr>")

        betting_signatures = []

        wins = 0
        losses = 0
        wins_amount = 0
        losses_amount = 0

        for x in result_bets:
            amount = x[4]
            openfield = str(x[11])
            betting_signatures.append(x[5]) #sig
            txid = x[5][:56]
            #print openfield

            rolled = roll(x[1],txid,roll_db,roll_cursor)

            if (rolled % 2 == 0) and (openfield == "even"): #if bets even and wins
                cell_color = "#cfe0e8"
                icon = "<img src=/static/green.png alt='green'>"
                result = "win"
                wins = wins + 1
                wins_amount = wins_amount + amount
            elif (rolled % 2 != 0) and (openfield == "odd"): #if bets odd and wins
                cell_color = "#cfe0e8"
                icon = "<img src=/static/green.png alt='green'>"
                result = "win"
                wins = wins + 1
                wins_amount = wins_amount + amount
            else:
                cell_color = "#87bdd8"
                icon = "<img src=/static/red.png alt='red'>"
                result = "loss"
                losses = losses + 1
                losses_amount = losses_amount + amount

            view_bets.append("<tr bgcolor="+cell_color+">")
            view_bets.append("<td><p3>{}</td>".format(x[0]))#block height
            view_bets.append("<td>{}</td>".format(time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(float(x[1])))))#time
            view_bets.append("<td>{}</td>".format(x[2]))#player
            view_bets.append("<td>{}</td>".format(x[5][:56]))#txid
            view_bets.append("<td>{}</td>".format(rolled))
            view_bets.append("<td>{}</td>".format(x[4]))
            view_bets.append("<td>{}</td>".format(x[11]))
            view_bets.append("<td>{} {}</p3></td>".format(icon,result))
            view_bets.append("</tr>")



        c.execute('SELECT * FROM transactions WHERE address = ? AND openfield LIKE ? AND block_height > ? ORDER BY block_height DESC, timestamp DESC LIMIT 1000;',(address,)+('%'+"payout"+'%',)+(block_anchor,)) #should work, needs testing
        result_payouts = c.fetchall()

        #print result_payouts
        view_payouts = []

        view_payouts.append("<tr bgcolor=white>")
        view_payouts.append("<td>Block Height</td><td>Time</td><td>Player</td><td>TXID</td><td>Amount</td>")
        view_payouts.append("</tr>")

        for x in result_payouts:
            #print betting_signatures
            if x[11].startswith("payout"):
                view_payouts.append("<tr bgcolor=#daebe8>")
                view_payouts.append("<td>{}</td>".format(x[0])) #block height
                view_payouts.append("<td>{}</td>".format(time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(float(x[1]))))) #time
                view_payouts.append("<td>{}</td>".format(x[3]))  #player
                view_payouts.append("<td>{}</td>".format(x[5][:56])) #txid
                view_payouts.append("<td>{}</td>".format(x[4])) #amount
                view_payouts.append("</tr>")



        html = []
        html.append("<!DOCTYPE html>")
        html.append("<html>")
        html.append("<link rel = 'icon' href = 'static/zircodice.ico' type = 'image/x-icon' / >")
        html.append('<link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.7/css/bootstrap.min.css" >')
        html.append("<head>")
        html.append("<meta http-equiv='refresh' content='60' >")
        html.append("<link rel='stylesheet' type='text/css' href='static/style_zircodice.css'>")
        html.append("</head>")
        html.append("<META http-equiv='cache-control' content='no-cache'>")
        html.append("<TITLE>ZircoDice</TITLE>")
        html.append("<body><body class='bg'><center>")
        html.append("<h1>Welcome to ZircoDice</h1>")
        html.append("<p>Please send any amount of coins lower than 100 to the address <strong>"+address+"</strong> and include the word '<strong>even</strong>' or '<strong>odd</strong>' in the OpenField data.<br> You are betting on a random number from 0 to 9 the casino rolls. 0 is considered an even number. Every transaction has it's own roll to prevent abuse.<br>If you win, you will receive 2x your bet. House returns 95% of your win minus fees. Payout happens after 10 blocks have passed.</p>")
        html.append("<p2>News: The outcome is now based on random number drawing, not on numbers included in blocks</p2>")
        html.append("<br>")
        html.append("<h1>Bets</h1>")

        html.append("<div class ='container-fluid'>")
        html.append("<table class='table table-responsive'>"+ str(''.join(view_bets))+"</table>")
        html.append("</table>")
        html.append("</div>")

        html.append("<h1>Payouts</h1>")

        html.append("<div class ='container-fluid'>")
        html.append("<table class='table table-responsive'>" + str(''.join(view_payouts)))
        html.append("</table>")
        html.append("</div>")

        html.append("<p>We are currently at block {} from {} ({} minutes ago)</p>".format(last_block_height,time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(float(last_timestamp))),int((time.time() - float(last_timestamp))/60)))

        html.append("<div class ='container'>")
        html.append("<table class='table table-responsive'>")

        html.append("<tr colspan='2' bgcolor='white'><th>Statistics for the last 1000 bets</th>")
        html.append("<tr bgcolor='#f1e3dd'><td>Player wins:</td><td>{} ({}%)</td>".format(wins,percentage_of(wins,losses)))
        html.append("<tr bgcolor='#f1e3dd'><td>Player losses: </td><td>{} ({}%)</td>".format(losses,percentage_of(losses,wins)))
        html.append("<tr bgcolor='#f1e3dd'><td>Wins amount: </td><td>{}</td>".format(wins_amount))
        html.append("<tr bgcolor='#f1e3dd'><td>Losses amount: </td><td>{}</td>".format(losses_amount))
        html.append("<tr bgcolor='#f1e3dd'><td>House balance: </td><td>{}</td>".format(balancesimple(c, address)))

        html.append("<tr bgcolor='#f1e3dd'><td>Odds rolled: </td><td>{}</td>".format(probability_count(roll_cursor)[1]))
        html.append("<tr bgcolor='#f1e3dd'><td>Evens rolled: </td><td>{}</td>".format(probability_count(roll_cursor)[0]))




        html.append("</table>")
        html.append("</div>")

        html.append("</body>")
        html.append("</html>")

        c.close()
        self.write(''.join(html))

def make_app():

    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": "static"}),
    ])


if __name__ == "__main__":
    app = make_app()
    app.listen(1212)
    print("Server starting...")
    tornado.ioloop.IOLoop.current().start()
