import sqlite3
import web
import time
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
import base64
import hashlib

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

        c.execute("select * from transactions where recipient = '" + address + "' ORDER BY block_height DESC, timestamp DESC LIMIT 100;")
        result_bets = c.fetchall()
        view_bets = []

        for x in result_bets:
            view_bets.append("<tr>")
            view_bets.append("<td>" + str(x[0]) + "</td>")
            view_bets.append("<td>" + str(time.strftime("%Y/%m/%d,%H:%M:%S", time.localtime(float(x[1])))))
            view_bets.append("<td>" + str(x[2]) + "</td>")
            view_bets.append("<td>" + str(x[4]) + "</td>")
            view_bets.append("<tr>")

        c.execute("select * from transactions where address = '" + address + "' ORDER BY block_height DESC, timestamp DESC LIMIT 100;")
        result_payouts = c.fetchall()
        view_payouts = []

        for x in result_payouts:
            view_payouts.append("<tr>")
            view_payouts.append("<td>" + str(x[0]) + "</td>")
            view_payouts.append("<td>" + str(time.strftime("%Y/%m/%d,%H:%M:%S", time.localtime(float(x[1])))))
            view_payouts.append("<td>" + str(x[3]) + "</td>")
            view_payouts.append("<td>" + str(x[4]) + "</td>")
            view_payouts.append("<tr>")

        c.close()

        html = "<!DOCTYPE html>" \
               "<html>" \
               "<head>" \
               "<meta http-equiv='refresh' content='60' >" \
               "<link rel='stylesheet' type='text/css' href='static/style.css'>" \
               "</head>" \
               "<META http-equiv='cache-control' content='no-cache'>" \
               "<TITLE>ZircoDice</TITLE>" \
               "<body><center>" \
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
