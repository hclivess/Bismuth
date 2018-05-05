import psutil
import time

import tornado.ioloop
import tornado.web

#print(psutil.pids()) # Print all pids


class MainHandler(tornado.web.RequestHandler):
    def get(self):

        pnames = []
        del pnames [:]
        for process in psutil.pids():
            try:
                # print (process)
                p = psutil.Process(process)  # The pid of desired process

                # print(p.name()) # If the name is "python.exe" is called by python
                # print(p.cmdline()) # Is the command line this process has been called with
                pnames.append(p.cmdline())


            except:
                pass

        if "node.py" in str(pnames):
            self.write("<font color='green'>")
            self.write("Node: <strong>OK</strong>")
            self.write("</font>")
            self.write("<br>")
        else:
            self.write("<font color='red'>")
            self.write("Node: <strong>Not running</strong>")
            self.write("</font>")
            self.write("<br>")

        if "zircodice_web.py" in str(pnames):
            self.write("<font color='green'>")
            self.write("Casino Interface: <strong>OK</strong>")
            self.write("</font>")
            self.write("<br>")
        else:
            self.write("<font color='red'>")
            self.write("Casino Interface: <strong>Not running</strong>")
            self.write("</font>")
            self.write("<br>")

        if "zircodice_dappie.py" in str(pnames):
            self.write("<font color='green'>")
            self.write("Casino Engine: <strong>OK</strong>")
            self.write("</font>")
            self.write("<br>")
        else:
            self.write("<font color='red'>")
            self.write("Casino Engine: <strong>Not running</strong>")
            self.write("</font>")
            self.write("<br>")

        if "ledger_explorer.py" in str(pnames):
            self.write("<font color='green'>")
            self.write("Ledger Explorer: <strong>OK</strong>")
            self.write("</font>")
            self.write("<br>")
        else:
            self.write("<font color='red'>")
            self.write("Ledger Explorer: <strong>Not running</strong>")
            self.write("</font>")
            self.write("<br>")

        if "wallet.py" in str(pnames):
            self.write("<font color='green'>")
            self.write("Wallet: <strong>OK</strong>")
            self.write("</font>")
            self.write("<br>")
        else:
            self.write("<font color='red'>")
            self.write("Wallet: <strong>Not running</strong>")
            self.write("</font>")
            self.write("<br>")

        if "anon_dappie.py" in str(pnames):
            self.write("<font color='green'>")
            self.write("Anonymizer: <strong>OK</strong>")
            self.write("</font>")
            self.write("<br>")
        else:
            self.write("<font color='red'>")
            self.write("Anonymizer: <strong>Not running</strong>")
            self.write("</font>")
            self.write("<br>")

        if "html_dappie.py" in str(pnames):
            self.write("<font color='green'>")
            self.write("HTML Interpreter: <strong>OK</strong>")
            self.write("</font>")
            self.write("<br>")
        else:
            self.write("<font color='red'>")
            self.write("HTML Interpreter: <strong>Not running</strong>")
            self.write("</font>")
            self.write("<br>")

        if "geo.py" in str(pnames):
            self.write("<font color='green'>")
            self.write("Geo: <strong>OK</strong>")
            self.write("</font>")
            self.write("<br>")
        else:
            self.write("<font color='red'>")
            self.write("Geo: <strong>Not running</strong>")
            self.write("</font>")
            self.write("<br>")

        if "bisdom.py" in str(pnames):
            self.write("<font color='green'>")
            self.write("Bisdom: <strong>OK</strong>")
            self.write("</font>")
            self.write("<br>")
        else:
            self.write("<font color='red'>")
            self.write("Bisdom: <strong>Not running</strong>")
            self.write("</font>")
            self.write("<br>")

        self.write("<br>")
        self.write("Last updated: {}".format(time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime(time.time()))))
        #time.sleep(5)



def make_app():
    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": "static"}),
    ])

if __name__ == "__main__":
    app = make_app()
    app.listen(80)
    tornado.ioloop.IOLoop.current().start()