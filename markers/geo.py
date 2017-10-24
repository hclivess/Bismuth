import requests, json, socks, connections

import tornado.ioloop
import tornado.web

with open('key.secret', 'r') as f: #get yours here: https://developers.google.com/maps/documentation/javascript/marker-clustering
    api_key = f.read()


class MainHandler(tornado.web.RequestHandler):
    def get(self):


        s = socks.socksocket()
        s.settimeout(10)
        s.connect(("127.0.0.1", 5658))
        connections.send(s, "statusget", 10)
        response = connections.receive(s, 10)
        s.close()

        nodes_list = response[2]
        ips = nodes_list

        markers = []

        print("IPs:",ips)

        with open('geo.json', 'w') as f:
            for ip in ips:
                getgeo = requests.request("GET", "http://freegeoip.net/json/{}".format(ip))
                response_web = json.loads(getgeo.text)
                try:
                    print(response_web).encode("utf-8")
                except:
                    pass


                markers.append("{{lat: {},".format(response_web["latitude"]))
                markers.append(" lng: {}}},\n".format(response_web["longitude"]))



        html = []
        html.append("<!DOCTYPE html>\n")
        html.append("<html>\n")
        html.append("<head>\n")
        html.append("<meta name='viewport' content='initial-scale=1.0, user-scalable=no'>\n")
        html.append("<meta charset='utf-8'>\n")
        html.append("<title>Bismuth Node Statistics</title>\n")
        html.append('<link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.7/css/bootstrap.min.css" >')
        html.append("<style>\n")
        html.append("/* Always set the map height explicitly to define the size of the div\n")
        html.append("* element that contains the map. */\n")
        html.append("#map {\n")
        html.append("height: 100%;\n")
        html.append("}\n")
        html.append("/* Optional: Makes the sample page fill the window. */\n")
        html.append("html, body {\n")
        html.append("height: 100%;\n")
        html.append("margin: 0;\n")
        html.append("padding: 0;\n")
        html.append("}\n")
        html.append("</style>\n")
        html.append("</head>\n")
        html.append("<body>\n")

        html.append("<div id='map'></div>\n")
        html.append("<script>\n")
        html.append("\n")
        html.append("function initMap() {\n")

        html.append("var map = new google.maps.Map(document.getElementById('map'), {\n")
        html.append("zoom: 3,\n")
        html.append("center: {lat: -28.024, lng: 140.887}\n")
        html.append("});\n")

        html.append("// Create an array of alphabetical characters used to label the markers.\n")
        html.append("var labels = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';\n")

        html.append("// Add some markers to the map.\n")
        html.append("// Note: The code uses the JavaScript Array.prototype.map() method to\n")
        html.append("// create an array of markers based on a given 'locations' array.\n")
        html.append("// The map() method here has nothing to do with the Google Maps API.\n")
        html.append("var markers = locations.map(function(location, i) {\n")
        html.append("return new google.maps.Marker({\n")
        html.append("position: location,\n")
        html.append("label: labels[i % labels.length]\n")
        html.append("});\n")
        html.append("});\n")

        html.append("// Add a marker clusterer to manage the markers.\n")
        html.append("var markerCluster = new MarkerClusterer(map, markers,\n")
        html.append("{imagePath: 'https://developers.google.com/maps/documentation/javascript/examples/markerclusterer/m'});\n")
        html.append("}\n")
        html.append("var locations = [\n")
        html.append(''.join(markers))
        html.append("]\n")
        html.append("</script>\n")
        html.append("<script src='https://developers.google.com/maps/documentation/javascript/examples/markerclusterer/markerclusterer.js'>\n")
        html.append("</script>\n")
        html.append("<script async defer\n")
        html.append("src='https://maps.googleapis.com/maps/api/js?key={}&callback=initMap'>".format(api_key))
        html.append("</script>\n")

        """
        node_address = response[0]
        nodes_count = response[1]
        nodes_list = response[2]
        threads_count = response[3]
        uptime = response[4]
        consensus = response[5]
        consensus_percentage = response[6]
        version = response[7]
        html.append("<div class = 'col-md-8'>")
        html.append("Node address: {}<br>".format(node_address))
        html.append("Number of nodes: {}<br>".format(nodes_count))
        html.append("List of nodes: {}<br>".format(nodes_list))
        html.append("Number of threads: {}<br>".format(threads_count))
        html.append("Uptime: {}<br>".format(uptime))
        html.append("Consensus: {}<br>".format(consensus))
        html.append("Consensus percentage: {}<br>".format(consensus_percentage))
        html.append("Version: {}<br>".format(version))
        html.append("</div>")
        """

        html.append("</body>\n")
        html.append("</html>\n")

        self.write(''.join(html))

def make_app():

    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": "static"}),
    ])

if __name__ == "__main__":
    app = make_app()
    app.listen(5493)
    tornado.ioloop.IOLoop.current().start()


