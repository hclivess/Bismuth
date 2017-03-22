import sqlite3
import web
import time

urls = (
    '/', 'index'
)

class index:
    def GET(self):

        # redraw chart
        conn = sqlite3.connect('static/ledger.db')
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
            axis0.append(x[0])  # append block height
            axis1.append(x[1])  # append timestamp
            axis4.append(x[4])  # append amount
            axis8.append(x[8])  # append fee
            axis9.append(x[9])  # append reward

        plotter = []

        plotter.append('<!doctype html>\n')
        plotter.append('<html>\n')
        plotter.append('<link rel = "icon" href = "static/explorer.ico" type = "image/x-icon" / >\n')
        plotter.append('<head>\n')
        #plotter.append('<meta http-equiv="refresh" content="60" >')
        plotter.append('<link rel="stylesheet" type="text/css" href="static/style.css">')
        plotter.append('<title>Transaction Explorer</title>\n')
        plotter.append('<script src="static/Chart.js"></script>\n')
        #plotter.append('<link rel="stylesheet" type="text/css" href="style.css">\n')
        plotter.append('</head>\n')
        plotter.append('<body bgcolor = "white">\n')

        # define canvas
        plotter.append('<canvas id="canvas" height="150" width="600"></canvas>\n')
        # define canvas

        plotter.append('<script>\n')

        # onload
        plotter.append("var ctx = document.getElementById('canvas').getContext('2d');")
        # onload

        # segment
        plotter.append("var canvas = new Chart(ctx, {\n")
        plotter.append("type: 'line',\n")
        plotter.append("data: {\n")
        plotter.append("labels: "+ str(map(str, axis0)) +",\n")
        plotter.append("datasets: [{\n")
        plotter.append("label: 'Timestamp progression',\n")
        plotter.append("data: "+ str(map(str, axis1)) +",\n")
        plotter.append("backgroundColor: 'rgba(153, 255, 51, 0.4)'\n")
        plotter.append("}, {\n")
        plotter.append("label: 'Spending in time',\n")
        plotter.append("hidden: true,\n")
        plotter.append("data: "+ str(map(str, axis4)) +",\n")
        plotter.append("backgroundColor: 'rgba(255, 153, 0, 0.4)'\n")
        plotter.append("}, {\n")
        plotter.append("label: 'Fee in time',\n")
        plotter.append("hidden: true,\n")
        plotter.append("data: "+ str(map(str, axis8)) +",\n")
        plotter.append("backgroundColor: 'rgba(63, 65, 191, 0.4)'\n")
        plotter.append("}, {\n")
        plotter.append("label: 'Reward in time',\n")
        plotter.append("hidden: true,\n")
        plotter.append("data: "+ str(map(str, axis9)) +",\n")
        plotter.append("backgroundColor: 'rgba(189, 63, 191, 0.4)'\n")
        plotter.append("}]\n")
        plotter.append("}\n")
        plotter.append("});\n")
        # segment

        plotter.append('</script>\n')
        plotter.append('</body>\n')
        plotter.append('</html>')
        # redraw chart

        conn = sqlite3.connect('static/ledger.db')
        c = conn.cursor()
        c.execute("SELECT * FROM transactions ORDER BY block_height DESC, timestamp DESC LIMIT 500;")

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
            #view.append("<td>" + str(x[10]) + "</td>")
            view.append("<tr>")
            i = i+1

        c.close()

        html = "<body><body background="'static/explorer_bg.png'"><center><center><h1>Bismuth Transaction Explorer</h1></center><p><a href='static/ledger.db'>download latest blockchain</a></p>"+ str(''.join(plotter)) +"<table style='width:100%' bgcolor='white'><tr><td>Block</td><td>Timestamp</td><td>From</td><td>To</td><td>Amount</td><td>Block Hash</td><td>Fee</td><td>Reward</td></tr>" + str(''.join(view)) +"</table></body></html>"

        return html

if __name__ == "__main__":
    app = web.application(urls, globals())
    app.run()
