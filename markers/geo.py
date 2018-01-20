import connections
import json
import requests
import socks

import tornado.ioloop
import tornado.web

with open('key.secret', 'r') as f:
    # get yours here:
    # https://developers.google.com/maps/documentation/javascript/marker-clustering
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

        print("IPs:", ips)

        with open('geo.json', 'w') as f:
            for node in nodes_list:
                try:
                    getgeo = requests.request("GET", "http://freegeoip.net/json/{}".format(ip))
                    response_web = json.loads(getgeo.text)
                    print(response_web).encode("utf-8")
                except:
                    pass

                markers = """
                {{lat: {latitude},
                lng: {longitude}}},
                """.format(latitude=response_web["latitude"],
                           longitude=response_web["longitude"])

        html = """
        <!DOCTYPE html>
        <html>
        <head>
        <meta name='viewport' content='initial-scale=1.0, user-scalable=no'>
        <meta charset='utf-8'>
        <title>Bismuth Node Statistics</title>
        <link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/3.3.7/css/bootstrap.min.css"
        <style>
        /* Always set the map height explicitly to define the size of the div
        * element that contains the map. */
        #map {
            height: 100%;
        }
        /* Optional: Makes the sample page fill the window. */
        html, body {
            height: 100%;
            margin: 0;
            padding: 0;
        }
        </style>
        </head>
        <body>

        <div id='map'></div>
        <script>
        function initMap() {

        var map = new google.maps.Map(document.getElementById('map'), {
            zoom: 3,
            center: {lat: -28.024, lng: 140.887}
        });

        // Create an array of alphabetical characters used to label the markers.
        var labels = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';

        // Add some markers to the map.
        // Note: The code uses the JavaScript Array.prototype.map() method to
        // create an array of markers based on a given 'locations' array.
        // The map() method here has nothing to do with the Google Maps API.
        var markers = locations.map(function(location, i) {
            return new google.maps.Marker({
                position: location,
                label: labels[i % labels.length]
                });
        });

        // Add a marker clusterer to manage the markers.
        var markerCluster = new MarkerClusterer(map, markers,
        {imagePath: 'https://developers.google.com/maps/documentation/javascript/examples/markerclusterer/m'});
        }
        var locations = [
            {markers} //''.join(markers))
        ]
        </script>
            <script src='https://developers.google.com/maps/documentation/javascript/examples/markerclusterer/markerclusterer.js'>
        </script>
        <script async defer
            src='https://maps.googleapis.com/maps/api/js?key={
            }&callback=initMap'>"
        </script>

        <div class = 'col-md-8'>
            Node address: {node_address}<br>
            Number of nodes: {nodes_count}<br>
            List of nodes: {nodes_list}<br>
            Number of threads: {threads_count}<br>
            Uptime: {uptime}<br>
            Consensus: {concensus}<br>
            Consensus percentage: {concensus_precentage}<br>
            Version: {version}<br>
        </div>
        </body>
        </html>
        """

        self.write(html.format(markers=markers,
                               api_key=api_key,
                               node_address=response[0],
                               nodes_count=response[1],
                               nodes_list=nodes_list,
                               threads_count=response[3],
                               uptime=response[4],
                               consensus=response[5],
                               consensus_percentage=response[6],
                               version=response[7]))


def make_app():
    return tornado.web.Application([
        (r"/", MainHandler),
        (r"/static/(.*)",
         tornado.web.StaticFileHandler,
         {"path": "static"}),
    ])


if __name__ == "__main__":
    app = make_app()
    app.listen(5493)
    tornado.ioloop.IOLoop.current().start()