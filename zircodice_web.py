import sqlite3
import web
import time
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
import base64
import hashlib
import re

key = RSA.importKey(open('privkey.der').read())
public_key = key.publickey()
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
public_key_hashed = base64.b64encode(public_key_readable)
address = hashlib.sha224(public_key_readable).hexdigest()

urls = (
    '/', 'index'
)

class index:
    def GET(self):

        # redraw chart
        conn = sqlite3.connect('./ledger.db')
        c = conn.cursor()

        c.execute("select * from transactions where openfield = '" + base64.b64encode("odd") + "' OR openfield = '" + base64.b64encode("even") + "' and recipient = '" + address + "' ORDER BY block_height DESC, timestamp DESC LIMIT 100;")
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
            if (digit_last % 2 == 0) and (openfield == base64.b64encode("odd")): #if bets odd and wins
                cell_color = "green"
                result = "win"
            elif (digit_last % 2 != 0) and (openfield == base64.b64encode("even")): #if bets even and wins
                cell_color = "green"
                result = "win"
            else:
                cell_color = "red"
                result = "loss"

            view_bets.append("<tr bgcolor="+cell_color+">")
            view_bets.append("<td>" + str(x[0]) + "</td>")#block height
            view_bets.append("<td>" + str(time.strftime("%Y/%m/%d,%H:%M:%S", time.localtime(float(x[1])))))#time
            view_bets.append("<td>" + str(x[2]) + "</td>") #player
            view_bets.append("<td>" + str(x[7]) + "</td>") #block hash
            view_bets.append("<td>" + str(digit_last) + "</td>")
            view_bets.append("<td>" + str(x[4]) + "</td>") #amount
            view_bets.append("<td>" + str(base64.b64decode(x[11])) + "</td>")
            view_bets.append("<td>" + result + "</td>")
            view_bets.append("<tr>")

        c.execute("select * from transactions where address = '" + address + "' ORDER BY block_height DESC, timestamp DESC LIMIT 100;")
        result_payouts = c.fetchall()
        #print result_payouts
        view_payouts = []

        view_payouts.append("<tr bgcolor=white>")
        view_payouts.append("<td>Block Height</td><td>Time</td><td>Player</td><td>Block Hash</td><td>Amount</td>")
        view_payouts.append("</tr>")

        for x in result_payouts:
            #print betting_signatures
            if x[11].startswith(base64.b64encode("payout")):
                view_payouts.append("<tr bgcolor=lightblue>")
                view_payouts.append("<td>" + str(x[0]) + "</td>") #block height
                view_payouts.append("<td>" + str(time.strftime("%Y/%m/%d,%H:%M:%S", time.localtime(float(x[1]))))) #time
                view_payouts.append("<td>" + str(x[3]) + "</td>")  #player
                view_payouts.append("<td>" + str(x[7]) + "</td>") #block hash
                view_payouts.append("<td>" + str(x[4]) + "</td>") #amount
                view_payouts.append("<tr>")

        c.close()

        html = "<!DOCTYPE html>" \
               "<html>" \
               "<link rel = 'icon' href = 'static/zircodice.ico' type = 'image/x-icon' / >" \
               "<head>" \
               "<meta http-equiv='refresh' content='60' >" \
               "<link rel='stylesheet' type='text/css' href='static/style_zircodice.css'>" \
               "</head>" \
               "<META http-equiv='cache-control' content='no-cache'>" \
               "<TITLE>ZircoDice</TITLE>" \
               "<body><body background="'static/bg.jpg'"><center>" \
               "<h1>Welcome to ZircoDice</h1>" \
               "<p>Please send any amount of coins lower than 100 to the address <strong>"+address+"</strong> and include the word '<strong>even</strong>' or '<strong>odd</strong>' in the OpenField data.<br> You are betting on the last number in the block hash where your bet is included. 0 is considered an odd number.<br>If you win, you will receive 2x your bet.</p>" \
               "<br>" \
               "<h1>Bets</h1>" \
               "<table style='width:100%'>"+ str(''.join(view_bets))+"</table>" \
               "<h1>Payouts</h1>" \
               "<table style='width:100%'>" + str(''.join(view_payouts)) + "</table>" \
               "</body>" \
               "</html>"

        return html

if __name__ == "__main__":
    app = web.application(urls, globals())
    app.run()