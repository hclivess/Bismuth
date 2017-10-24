import requests, json, threading
from flask import Flask

app = Flask(__name__)
sem = threading.Semaphore()


api_key = "AIzaSyDChTwxWzhywEftxffmpLfuU-mOAXObTKM"

ips = ['185.206.146.219', '46.246.31.54', '13.229.1.64', '94.113.207.67', '216.231.133.150', '62.153.176.78', '185.203.118.56', '85.217.171.9', '198.20.101.81', '82.165.146.7', '79.115.161.187', '176.31.245.46', '85.217.170.190', '52.74.167.222', '203.86.205.98', '79.248.164.214', '118.190.203.224', '72.140.110.221', '84.164.207.191', '87.26.113.161', '86.126.118.179', '185.177.59.161', '117.135.212.68', '158.106.98.168', '46.16.187.37']
#responses = []
markers = []

with open('geo.json', 'w') as f:
    for ip in ips:
        getgeo = requests.request("GET","http://freegeoip.net/json/{}".format(ip))
        response = json.loads(getgeo.text)
        for k, v in response.items():
            if k == "latitude":
                markers.append("{{lat: {},".format(v))
            if k == "longitude":
                markers.append(" lng: {}}},\n".format(v))

                #{lat: -31.563910, lng: 147.154312}

print(''.join(markers))



        #responses.append(response)

#print (responses)


@app.route('/')
def main():
    sem.acquire()

    html = []
    html.append("<!DOCTYPE html>\n")
    html.append("<html>\n")
    html.append("<head>\n")
    html.append("<meta name='viewport' content='initial-scale=1.0, user-scalable=no'>\n")
    html.append("<meta charset='utf-8'>\n")
    html.append("<title>Marker Clustering</title>\n")
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
    #html.append("{lat: -31.563910, lng: 147.154312},\n")
    #html.append("{lat: -33.718234, lng: 150.363181},\n")
    #html.append("{lat: -33.727111, lng: 150.371124},\n")
    #html.append("{lat: -43.999792, lng: 170.463352}\n")
    html.append(''.join(markers))
    html.append("]\n")
    html.append("</script>\n")
    html.append("<script src='https://developers.google.com/maps/documentation/javascript/examples/markerclusterer/markerclusterer.js'>\n")
    html.append("</script>\n")
    html.append("<script async defer\n")
    html.append("src='https://maps.googleapis.com/maps/api/js?key={}&callback=initMap'>".format(api_key))
    html.append("</script>\n")
    html.append("</body>\n")
    html.append("</html>\n")

    sem.release()
    return ''.join(html)

