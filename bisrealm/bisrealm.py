import requests
import json
import tornado.ioloop
import tornado.web
import math

def percentage_of(part, whole):
    result = 100 * (part / whole)
    return result

class MainHandler(tornado.web.RequestHandler):
    def get(self):
        html = []
        html.append ("<!DOCTYPE html>")
        html.append ("<html>")
        html.append ("<head>")
        html.append ("<link rel='stylesheet' type='text/css' href='static/mana.css'>")
        html.append ("<title>Page Title</title>")
        html.append ("</head>")
        html.append ("<body>")

        html.append ("<body background = 'static/grass.jpg'>")

        #build homes
        rich_raw = requests.get ('https://bismuth.online/api/richlist/all').text
        rich = json.loads (rich_raw)
        richest = float(rich[0]['balance'])

        for key in rich:
            address = ("{} {}".format(key['alias'],key['address'][:10]))
            balance = float ((key['balance']))
            whale_factor = int(percentage_of (balance, richest))
            print(whale_factor)

            if whale_factor > 0:
                html.append ('<div class="gallery">')
                html.append ('<img src="static/house{}.png" alt="Modest House">'.format(whale_factor))
                html.append ('<div class="desc">{}</div>'.format(address))
                html.append ('</div>')



        # build homes

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


