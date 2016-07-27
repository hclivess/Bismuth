import web
import sqlite3

urls = (
    '/', 'index'
)

class index:
    def GET(self):

        conn = sqlite3.connect('../ledger.db')
        c = conn.cursor()

        view = []

        c.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
        imax = c.fetchone()[0]
        imax = 50 #latest 50 only
        print "imax:"+str(imax)
        i=0
        while i < imax:
            for row in c.execute('SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1 OFFSET '+str(i)+';'):
                block_height = row[0]
                address = row[2]
                to_address = row[3]
                amount = row[4]
                print address
                print to_address
                print amount
                view.append("<tr>")
                view.append("<td>"+str(block_height)+"</td>")
                view.append("<td>"+address+"</td>")
                view.append("<td>"+to_address+"</td>")
                view.append("<td>"+amount+"</td>")
                view.append("<tr>")
                i = i+1

        c.close()

        html="<!DOCTYPE html>"\
              "<html>"\
              "<head>"\
              "<meta http-equiv='refresh' content='60' >"\
              "<link rel='stylesheet' type='text/css' href='static/style.css'>"\
              "</head>"\
              "<META http-equiv='cache-control' content='no-cache'>"\
              "<TITLE>Transaction Explorer</TITLE>"\
              "<body><table style='width:100%'><tr><td>No.</td><td>From</td><td>To</td><td>Amount</td></tr>"+str(''.join(view))+\
              "</table></body>"\
              "</html>"\
              
        return html

if __name__ == "__main__":
    app = web.application(urls, globals())
    app.run()
