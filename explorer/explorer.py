import sqlite3
import web

urls = (
    '/', 'index'
)

class index:
    def GET(self):
        conn = sqlite3.connect('./ledger.db')
        c = conn.cursor()
        c.execute("SELECT * FROM transactions ORDER BY block_height DESC;")

        all = c.fetchall()

        view = []

        for x in all:
            view.append("<tr>")
            view.append("<td>" + str(x[0]) + "</td>")
            view.append("<td>" + str(x[1]) + "</td>")
            view.append("<td>" + str(x[2]) + "</td>")
            view.append("<td>" + str(x[3]) + "</td>")
            view.append("<td>" + str(x[4]) + "</td>")
            #view.append("<td>" + str(x[5]) + "</td>")
            #view.append("<td>" + str(x[6]) + "</td>")
            view.append("<td>" + str(x[7]) + "</td>")
            view.append("<td>" + str(x[8]) + "</td>")
            view.append("<td>" + str(x[9]) + "</td>")
            view.append("<tr>")

        c.close()

        html = "<!DOCTYPE html>" \
               "<html>" \
               "<head>" \
               "<meta http-equiv='refresh' content='60' >" \
               "<link rel='stylesheet' type='text/css' href='static/style.css'>" \
               "</head>" \
               "<META http-equiv='cache-control' content='no-cache'>" \
               "<TITLE>Transaction Explorer</TITLE>" \
               "<body><table style='width:100%'><tr><td>Block</td><td>Timestamp</td><td>From</td><td>To</td><td>Amount</td><td>Transaction Hash</td><td>Fee</td><td>Reward</td></tr>" + str(
            ''.join(view)) + \
               "</table></body>" \
               "</html>"

        return html

if __name__ == "__main__":
    app = web.application(urls, globals())
    app.run()
