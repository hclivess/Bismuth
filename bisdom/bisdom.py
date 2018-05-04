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
        html = []
        html.append ("<!DOCTYPE html>")
        html.append ("<html>")
        html.append ("<head>")
        html.append ("<link rel='stylesheet' type='text/css' href='static/bisdom.css'>")
        html.append ("<title>Page Title</title>")
        html.append ("</head>")
        html.append ("<body>")

        html.append ("<body background = 'static/grass.jpg'>")

        #build homes
        rich_raw = requests.get ('https://bismuth.online/api/richlist/all').text
        rich = json.loads (rich_raw)
        richest = float(rich[0]['balance'])

        html.append ('<table class="headline">')
        html.append ('<h1>Residential District</h1>')

        tds = 0
        for key in rich:
            address = ("{} {}".format(key['alias'],key['address'][:10]))
            balance = float ((key['balance']))
            whale_factor = int(percentage_of (balance, richest))


            if whale_factor > 0:
                if tds == 0:
                    html.append ('<tr>')

                print (whale_factor)

                html.append ('<td class="gallery">')
                html.append ('<img src="static/house{}.png" alt="Modest House">'.format(whale_factor))
                html.append ('<div class="desc">{}</div>'.format(address))
                html.append ('</td>')

                tds = tds + 1
                print ("tds", tds)
                if tds == 5:
                    html.append ('</tr>')
                    tds = 0

        html.append ('</table>')

        # build homes








        miners_raw = requests.get ('https://bismuth.online/api/miners/all').text
        miners = json.loads (miners_raw)
        miner_top = float(miners[0]['rewards'])

        html.append ('<table class="headline">')
        html.append ('<h1>Industrial District</h1>')

        tds = 0
        for key in miners:
            address = ("{} {}".format(key['alias'],key['address'][:10]))
            rewards = float ((key['rewards']))
            whale_factor = roundup(int(percentage_of (rewards, miner_top)))



            if whale_factor > 0:
                if tds == 0:
                    html.append ('<tr>')

                print (whale_factor)

                html.append ('<td class="gallery">')

                html.append ('<img src="static/miners/mine{}.png" alt="Modest House">'.format(whale_factor))
                html.append ('<div class="desc">{}</div>'.format(address))
                html.append ('</td>')

                tds = tds + 1
                print ("tds", tds)
                if tds == 5:
                    html.append ('</tr>')
                    tds = 0

        html.append ('</table>')


        html.append ("</body>")
        html.append ("</html>")
        self.write(''.join(html))

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


