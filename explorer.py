import sqlite3
import web
import time

urls = (
    '/', 'index'
)

class index:
    def GET(self):

        # redraw chart
        conn = sqlite3.connect('./ledger.db')
        c = conn.cursor()
        c.execute("SELECT * FROM transactions ORDER BY block_height DESC, timestamp DESC LIMIT 100;")

        all = c.fetchall()[::-1]

        axis0 = []
        axis1 = []
        axis4 = []
        axis8 = []
        axis9 = []
        axis10 = []

        i = 1
        for x in all:
            axis0.append(x[0])  # append timestamp
            axis1.append(x[1])  # append block height

            axis4.append(x[4])  # append amount
            axis8.append(x[8])  # append fee
            axis9.append(x[9])  # append reward
            axis10.append(x[10])  # append confirmations

        output = "static/plotter.html"
        f = open(output, 'w')

        f.write('<!doctype html>\n')
        f.write('<html>\n')
        f.write('<head>\n')
        f.write('<title>Line Chart</title>\n')
        f.write('<script src="Chart.js"></script>\n')
        f.write('<link rel="stylesheet" type="text/css" href="style.css">\n')
        f.write('</head>\n')
        f.write('<body bgcolor = "white">\n')

        # define canvas
        f.write('<canvas id="canvas" height="150" width="600"></canvas>\n')
        # define canvas

        f.write('<script>\n')

        # onload
        f.write("var ctx = document.getElementById('canvas').getContext('2d');")
        # onload

        # segment
        f.write("var canvas = new Chart(ctx, {")
        f.write("type: 'line',")
        f.write("data: {")
        f.write("labels: "+ str(map(str, axis0)) +",")
        f.write("datasets: [{")
        f.write("label: 'Timestamp progression',")
        f.write("data: "+ str(map(str, axis1)) +",")
        f.write("backgroundColor: 'rgba(153, 255, 51, 0.4)'")
        f.write("}, {")
        f.write("label: 'Spending in time',")
        f.write("data: "+ str(map(str, axis4)) +",")
        f.write("backgroundColor: 'rgba(255, 153, 0, 0.4)'")
        f.write("}, {")
        f.write("label: 'Fee in time',")
        f.write("data: "+ str(map(str, axis8)) +",")
        f.write("backgroundColor: 'rgba(63, 65, 191, 0.4)'")
        f.write("}, {")
        f.write("label: 'Reward in time',")
        f.write("data: "+ str(map(str, axis9)) +",")
        f.write("backgroundColor: 'rgba(189, 63, 191, 0.4)'")
        f.write("}]")
        f.write("}")
        f.write("});")
        # segment

        f.write('</script>\n')
        f.write('</body>\n')
        f.write('</html>')

        f.close()
        # redraw chart

        conn = sqlite3.connect('./ledger.db')
        c = conn.cursor()
        c.execute("SELECT * FROM transactions ORDER BY block_height DESC, timestamp DESC LIMIT 1000;")

        all = c.fetchall()

        view = []
        i = 0
        for x in all:
            if i % 2 == 0:
                color_cell = "#E8E8E8"
            else:
                color_cell = "white"
            view.append("<tr bgcolor ="+color_cell+">")
            view.append("<td>" + str(x[0]) + "</td>")
            view.append("<td>" + str(time.strftime("%Y/%m/%d,%H:%M:%S", time.localtime(float(x[1])))))
            view.append("<td>" + str(x[2]) + "</td>")
            view.append("<td>" + str(x[3].encode('utf-8')) + "</td>")
            view.append("<td>" + str(x[4]) + "</td>")
            #view.append("<td>" + str(x[5]) + "</td>")
            #view.append("<td>" + str(x[6]) + "</td>")
            view.append("<td>" + str(x[7]) + "</td>")
            view.append("<td>" + str(x[8]) + "</td>")
            view.append("<td>" + str(x[9]) + "</td>")
            view.append("<td>" + str(x[10]) + "</td>")
            view.append("<tr>")
            i = i+1

        c.close()

        html = "<!DOCTYPE html>" \
               "<html>" \
               "<link rel = 'icon' href = 'static/explorer.ico' type = 'image/x-icon' / >" \
               "<head>" \
               "<meta http-equiv='refresh' content='60' >" \
               "<link rel='stylesheet' type='text/css' href='static/style.css'>" \
               "</head>" \
               "<META http-equiv='cache-control' content='no-cache'>" \
               "<TITLE>Transaction Explorer</TITLE>" \
               "<body><body background="'static/explorer_bg.png'"><center>" \
               "<center><h1>Bismuth Transaction Explorer</h1></center><iframe src='static/plotter.html' width='100%' height='550'></iframe><table style='width:100%' bgcolor='white'><tr><td>Block</td><td>Timestamp</td><td>From</td><td>To</td><td>Amount</td><td>Block Hash</td><td>Fee</td><td>Reward</td><td>Confirmations</td></tr>" + str(''.join(view)) + \
               "</table></body>" \
               "</html>"

        return html

if __name__ == "__main__":
    app = web.application(urls, globals())
    app.run()
