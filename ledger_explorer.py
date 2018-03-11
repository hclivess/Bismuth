import sqlite3, time, options, random, sys

import tornado.ioloop
import tornado.web

config = options.Get()
config.read()
full_ledger = config.full_ledger_conf
hyper_path = config.hyper_path_conf
#hyper_path = "backup.db"
version = config.version_conf

if "testnet" in version:
    port = 2829
    full_ledger = 0
    hyper_path = "static/test.db"

def execute(cursor, query):
    """Secure execute for slow nodes"""
    while True:
        try:
            cursor.execute(query)
            break
        except Exception as e:
            print("Database query: {} {}".format(cursor, query))
            print("Database retry reason: {}".format(e))
            time.sleep(random.uniform(1, 3))
    return cursor

class MainHandler(tornado.web.RequestHandler):
    def get(self):
        # redraw chart

        #if full_ledger == 1:
        #    conn = sqlite3.connect(ledger_path)
        #else:

        conn = sqlite3.connect(hyper_path)

        c = conn.cursor()
        execute(c, "SELECT * FROM transactions ORDER BY block_height DESC, timestamp DESC LIMIT 100;")
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
            axis10.append(len(str(x)))  # tx size

        plotter = []

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
        plotter.append("labels: " + str(list(map(str, axis0))) + ",\n")
        plotter.append("datasets: [{\n")
        plotter.append("label: 'Timestamp progression',\n")
        plotter.append("data: " + str(list(map(str, axis1))) + ",\n")
        plotter.append("backgroundColor: 'rgba(153, 255, 51, 0.4)'\n")
        plotter.append("}, {\n")
        plotter.append("label: 'Spending',\n")
        plotter.append("hidden: true,\n")
        plotter.append("data: " + str(list(map(str, axis4))) + ",\n")
        plotter.append("backgroundColor: 'rgba(255, 153, 0, 0.4)'\n")
        plotter.append("}, {\n")
        plotter.append("label: 'Fee',\n")
        plotter.append("hidden: true,\n")
        plotter.append("data: " + str(list(map(str, axis8))) + ",\n")
        plotter.append("backgroundColor: 'rgba(63, 65, 191, 0.4)'\n")
        plotter.append("}, {\n")


        plotter.append("label: 'Transaction size',\n")
        plotter.append("hidden: true,\n")
        plotter.append("data: " + str(list(map(str, axis10))) + ",\n")
        plotter.append("backgroundColor: 'rgba(300, 50, 0, 0.4)'\n")
        plotter.append("}, {\n")

        plotter.append("label: 'Reward',\n")
        plotter.append("hidden: true,\n")
        plotter.append("data: " + str(list(map(str, axis9))) + ",\n")
        plotter.append("backgroundColor: 'rgba(189, 63, 191, 0.4)'\n")
        plotter.append("}]\n")
        plotter.append("}\n")
        plotter.append("});\n")
        # segment

        plotter.append('</script>\n')
        # plotter.append('</body>\n')
        # plotter.append('</html>')
        # redraw chart

        execute(c, "SELECT * FROM transactions ORDER BY block_height DESC, timestamp DESC LIMIT 500;")
        all = c.fetchall()

        tx_count = 0
        for x in all:
            if x[9] == 0: #if reward is 0
                tx_count = tx_count + 1

        transferred_total = 0
        for x in all:
            if x[9] == 0:  # if reward is 0
                transferred_total = transferred_total + x[4]


        execute(c, "SELECT difficulty FROM misc ORDER BY block_height DESC LIMIT 500;")
        diffs = c.fetchall()

        view = []
        i = 0
        b = -1

        for x in all:

            try: #first run
                x_old
            except:
                x_old = "init"

            if x[0] != x_old:
                color_cell = "#E8E8E8"
                view.append("<tr><td></td></tr>") #block separator
            else:
                color_cell = "white"


            view.append("<tr bgcolor ={}>".format(color_cell))

            if x[0] != x_old:
                b = b + 1


            if x_old != x[0]:
                view.append("<td>{}</td>".format(x[0])) #block height
            else:
                view.append("<td></td>")


            view.append("<td>{}".format(time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(float(x[1])))))
            view.append("<td>{}</td>".format(x[2]))
            view.append("<td>{}</td>".format(x[3]))
            view.append("<td>{}</td>".format('%.8f' % float(x[4])))
            view.append("<td>{}</td>".format(x[5][:56]))

            if x_old != x[0]:
                view.append("<td>{}</td>".format(x[7])) #block hash
            else:
                view.append("<td></td>")


            view.append("<td>{}</td>".format(x[8]))
            view.append("<td>{}</td>".format('%.6f' % float(x[9])))

            if x_old != x[0]:
                view.append("<td>{}</td>".format('%.10f' % float(diffs[b][0])))
            else:
                view.append("<td></td>")

            view.append("<tr>")

            x_old = x[0]
            i = i + 1


        c.close()

        html = []
        html.append('<!doctype html>\n')
        html.append('<html>\n')

        html.append('<head>\n')
        # plotter.append('<meta http-equiv="refresh" content="60" >')

        html.append('<title>Transaction Explorer</title>\n')
        # html.append('<link rel="stylesheet" type="text/css" href="static/style.css">')
        html.append('<link rel = "icon" href = "static/explorer.ico" type = "image/x-icon" / >\n')
        html.append('<link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.7/css/bootstrap.min.css" >')
        html.append('<script src="static/Chart.js"></script>\n')
        html.append('<script src="https://ajax.googleapis.com/ajax/libs/jquery/3.2.1/jquery.min.js"></script>')
        html.append('<script src="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.7/js/bootstrap.min.js"></script >')

        html.append('</head>\n')
        html.append('<body bgcolor = "white">\n')
        html.append("<body>")
        html.append("<body background='static/explorer_bg.png'>")

        html.append("<div class ='container-fluid'>")

        html.append("<div class='row'>")
        html.append("<center><h1>Bismuth Transaction Explorer</h1><p></center>")

        html.append("<div style='padding: 20px;'>")
        html.append("<center><a href='http://bismuth.cz/ledger.tar.gz' class='btn btn-info' role='button'>Download Blockchain</a></center>")
        html.append("</div>")

        html.append("</div>")

        html.append("<div class ='row'>")
        html.append(''.join(plotter))
        html.append("</div>")

        data_total = str(sys.getsizeof(str(all))/1024)
        html.append("<div class ='container-fluid'>")
        html.append("<table class='table table-responsive'>")

        html.append("<tr><th>Statistics for the last 500 blocks</th>")
        html.append("<tr><td>Size in KB: </td><td>{}</td>".format(data_total))
        html.append("<tr><td>Transactions: </td><td>{}</td>".format(tx_count))
        html.append("<tr><td>Transactions per block: </td><td>{}</td>".format(tx_count/500))
        html.append("<tr><td>Total BIS transferred: </td><td>{}</td>".format(transferred_total))

        html.append("</table>")
        html.append("</div>")


        html.append("<div class ='row'>")
        html.append("<table class='table table-responsive'>")
        html.append("<tr bgcolor='white'>")

        html.append("<td>Block</td>")
        html.append("<td>Timestamp</td>")
        html.append("<td>From</td>")
        html.append("<td>To</td>")
        html.append("<td>Amount</td>")
        html.append("<td>TXID</td>")
        html.append("<td>Block Hash</td>")
        html.append("<td>Fee</td>")
        html.append("<td>Reward</td>")
        html.append("<td>Difficulty</td>")
        html.append("</tr>")
        html.append(''.join(view))
        html.append("</table>")
        html.append("</div>")

        html.append("</div>")

        html.append("</body>")
        html.append("</html>")

        self.write(''.join(html))
        #self.render("ex.html", data=data)

def make_app():

    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": "static"}),
    ])


if __name__ == "__main__":
    app = make_app()
    app.listen(5492)
    tornado.ioloop.IOLoop.current().start()