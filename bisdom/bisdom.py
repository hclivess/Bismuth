import requests
import json
import tornado.ioloop
import tornado.web
import math

def roundup(x):
    return int(math.ceil(x / 10.0)) * 10

def percentage_of(part, whole):
    result = 100 * (part / whole)
    return result

class MainHandler(tornado.web.RequestHandler):
    def get(self):

        self.write ("<!DOCTYPE html>")
        self.write ("<html>")
        self.write ("<head>")
        self.write ("<link rel='stylesheet' type='text/css' href='static/bisdom.css'>")
        self.write ("<title>Bisdom</title>")
        self.write ("</head>")
        self.write ("<body>")

        self.write ("<body background = 'static/grass.jpg'>")

        #build homes
        rich_raw = requests.get ('https://bismuth.online/api/richlist/all').text
        rich = json.loads (rich_raw)
        richest = float(rich[0]['balance'])

        self.write ('<table class="headline">')
        self.write ('<h1>Residential District</h1>')

        tds = 0
        for key in rich:
            address = ("{} {}".format(key['alias'],key['address'][:10]))
            balance = float ((key['balance']))
            whale_factor = int(percentage_of (balance, richest))


            if whale_factor > 0:
                if tds == 0:
                    self.write ('<tr>')

                print (whale_factor)

                self.write ('<td class="gallery">')
                self.write ('<img src="static/holders/house{}.png" alt="{}">'.format(whale_factor,balance))
                self.write ('<div class="desc">{}</div>'.format(address))
                self.write ('</td>')

                tds = tds + 1
                print ("tds", tds)
                if tds == 5:
                    self.write ('</tr>')
                    tds = 0

        self.write ('</table>')

        # build homes








        miners_raw = requests.get ('https://bismuth.online/api/miners/all').text
        miners = json.loads (miners_raw)
        miner_top = float(miners[0]['rewards'])

        self.write ('<table class="headline">')
        self.write ('<h1>Industrial District</h1>')

        tds = 0
        for key in miners:
            address = ("{} {}".format(key['alias'],key['address'][:10]))
            rewards = float ((key['rewards']))
            whale_factor = roundup(int(percentage_of (rewards, miner_top)))



            if whale_factor > 0:
                if tds == 0:
                    self.write ('<tr>')

                print (whale_factor)

                self.write ('<td class="gallery">')

                self.write ('<img src="static/miners/mine{}.png" alt="{}">'.format(whale_factor, rewards))
                self.write ('<div class="desc">{}</div>'.format(address))
                self.write ('</td>')

                tds = tds + 1
                print ("tds", tds)
                if tds == 5:
                    self.write ('</tr>')
                    tds = 0

        self.write ('</table>')


        self.write ("</body>")
        self.write ("</html>")

def make_app():

    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": "static"}),
    ])

if __name__ == "__main__":
    app = make_app()
    app.listen(9090)
    print("Server starting...")
    tornado.ioloop.IOLoop.current().start()


