import psutil
import time

import tornado.ioloop
import tornado.web

_processes_ = [
    ["Node", "node"],
    ["Casino Interface", "zircodice_web.py"],
    ["Casino Engine", "zircodice_dappie.py"],
    ["Ledger Explorer", "ledger_explorer.py"],
    ["Wallet", "wallet.py"],
    ["Anonymizer", "anon_dappie.py"],
    ["HTML Interpreter", "html_dappie.py"],
    ["Geo", "geo.py"],
    ["Bisdom", "bisdom.py"],
    ["Twitter Bot", "twitterizer.py"],
    ["Wallet Server", "wallet_server.py"]]


class MainHandler(tornado.web.RequestHandler):
    def write_status(self, name, stat):
        color = 'green' if stat else 'red'
        stat = 'OK' if stat else 'Not running'
        self.write("<font color='{}'>"\
                   "{}: <strong>{}</strong>"\
                   "</font>"\
                   "<br>".format(color, stat, name))
            
    def get(self):
        pnames = []
        del pnames [:]
        for process in psutil.pids():
            try:
                p = psutil.Process(process)  # The pid of desired process
                pnames.append(p.cmdline())
            except:
                pass
    
        for process in _processes_:
            name, fn = process
            self.write_status(name, fn in str(pnames))
            
        last_updated = time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(time.time()))
        self.write("<br>"\
                   "Last updated: {}".format(last_updated))


def make_app():
    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": "static"}),
    ])


if __name__ == "__main__":
    app = make_app()
    app.listen(80)

    tornado.ioloop.IOLoop.current().start()
