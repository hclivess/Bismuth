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
        c.execute("SELECT * FROM transactions ORDER BY block_height DESC, timestamp DESC LIMIT 100;")

        c.execute("select * from transactions where recipient = '" + address + "'")
        result_bets = c.fetchall()

        view = []

        for x in result_bets:
            view.append("<tr>")
            view.append("<td>" + str(x[0]) + "</td>")
            view.append("<td>" + str(time.strftime("%Y/%m/%d,%H:%M:%S", time.localtime(float(x[1])))))
            view.append("<td>" + str(x[2]) + "</td>")
            #view.append("<td>" + str(x[3].encode('utf-8')) + "</td>")
            view.append("<td>" + str(x[4]) + "</td>")
            #view.append("<td>" + str(x[5]) + "</td>")
            #view.append("<td>" + str(x[6]) + "</td>")
            #view.append("<td>" + str(x[7]) + "</td>")
            #view.append("<td>" + str(x[8]) + "</td>")
            #view.append("<td>" + str(x[9]) + "</td>")
            #view.append("<td>" + str(x[10]) + "</td>")
            view.append("<tr>")

        c.execute("select * from transactions where recipient = '" + address + "'")
        result_payouts = c.fetchall()

        c.close()

        html = "<!DOCTYPE html>" \
               "<html>" \
               "<head>" \
               "<meta http-equiv='refresh' content='60' >" \
               "<link rel='stylesheet' type='text/css' href='static/style.css'>" \
               "</head>" \
               "<META http-equiv='cache-control' content='no-cache'>" \
               "<TITLE>Transaction Explorer</TITLE>" \
               "<body><center><h1>Beta</h1><p><table style='width:100%'>"+ str(''.join(view)) + \
               "</table></body>" \
               "</html>"

        return html

if __name__ == "__main__":
    app = web.application(urls, globals())
    app.run()
