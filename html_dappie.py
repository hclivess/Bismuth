import sqlite3, time, options, re
import log
import tornado.ioloop
import tornado.web


def replace_regex(string, replace):
    replaced_string = re.sub(r'^{}'.format(replace), "", string)
    return replaced_string


def html_update(file, mode, app_log):
    if mode not in ("normal","reindex"):
        raise ValueError ("Wrong value for html_update function")

    html = sqlite3.connect(file)
    html.text_factory = str
    h = html.cursor()
    h.execute("CREATE TABLE IF NOT EXISTS transactions (block_height INTEGER, timestamp NUMERIC, address, recipient, txid, content)")
    html.commit()

    if mode == "reindex":
        app_log.warning("HTML database will be reindexed")
        h.execute("DELETE FROM html")
        html.commit()

    h.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
    try:
        html_last_block = int(h.fetchone()[0])
    except:
        html_last_block = 0

    app_log.warning("HTML anchor block: {}".format(html_last_block))

    conn = sqlite3.connect('static/ledger.db')
    conn.text_factory = str
    c = conn.cursor()

    c.execute("SELECT * FROM transactions WHERE block_height >= ? AND openfield LIKE ?", (html_last_block,) + ("html=" + '%',))
    results = c.fetchall()

    print ("results",results)

    for row in results:
        content = replace_regex(row[11],"html=")
        txid = row[5][:56]

        try:
            h.execute("SELECT * from transactions WHERE txid = ?", (txid,))
            dummy = h.fetchall()[0] #check for uniqueness
            app_log.warning("HTML tx already processed: {} ({})".format(txid,dummy))
        except:

            h.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?)", (row[0], row[1], row[2], row[3], txid, content))
            html.commit()



class MainHandler(tornado.web.RequestHandler):
    def get(self):

        config = options.Get()
        config.read()

        # redraw chart
        html = sqlite3.connect("html.db")
        html.text_factory = str
        h = html.cursor()

        html = []
        print("selecting")

        for row in h.execute("SELECT * FROM transactions ORDER BY block_height DESC"):

            self.write("Block: ")
            self.write(str(row[0]))
            self.write("<br>")
            self.write("Time: ")
            self.write(time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(float(row[1]))))
            self.write("<br>")
            self.write("Author: ")
            self.write(str(row[2]))
            self.write("<br>")
            self.write("Content: ")
            self.write("<br><br>")


            content_safe = row[5].replace("<script", "(")
            content_safe = content_safe.replace("script>", ")")
            content_safe = content_safe.replace("http-equiv", "/http-equiv/")
            content_safe = content_safe.replace("onload", "/onload/")

            self.write(replace_regex(str(content_safe),"html="))
            self.write("<br><br>")




        #self.write(joined)


def make_app():
    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": "static"}),
    ])

if __name__ == "__main__":
    app_log = log.log("html.log", "WARNING", True)
    html_update("html.db","normal",app_log)

    app = make_app()
    app.listen(4585)
    tornado.ioloop.IOLoop.current().start()