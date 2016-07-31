import sqlite3

conn = sqlite3.connect('./ledger.db')
c = conn.cursor()
c.execute("SELECT * FROM transactions ORDER BY block_height DESC LIMIT 100;")

all = c.fetchall()[::-1]

axis0 = []
axis1 = []
axis4 = []
axis8 = []
axis9 = []


i = 1
for x in all:
    axis0.append(x[0]) # append timestamp
    axis1.append(x[1]) # append block height

    axis4.append(x[4])  # append amount
    axis8.append(x[8])  # append fee
    axis9.append(x[9])  # append reward

output = "static/plotter.html"
f = open(output, 'w')

f.write('<!doctype html>\n')
f.write('<html>\n')
f.write('<head>\n')
f.write('<title>Line Chart</title>\n')
f.write('<script src="Chart.js"></script>\n')
f.write('<link rel="stylesheet" type="text/css" href="style.css">\n')
f.write('</head>\n')
f.write('<body>\n')
f.write('<div style="width:100%">\n')
f.write('<div>\n')
#define canvas
f.write("<h1>Timestamp progression</h1>")
f.write('<canvas id="canvas" height="150" width="600"></canvas>\n')

f.write("<h1>Spending in time</h1>")
f.write('<canvas id="canvas2" height="150" width="600"></canvas>\n')

f.write("<h1>Fee in time</h1>")
f.write('<canvas id="canvas3" height="150" width="600"></canvas>\n')

f.write("<h1>Reward in time</h1>")
f.write('<canvas id="canvas4" height="150" width="600"></canvas>\n')
#define canvas
f.write('</div>\n')
f.write('</div>\n')
f.write('<script>\n')
f.write('var randomScalingFactor = function(){ return Math.round(Math.random()*100)};\n')

#onload
f.write('window.onload = function(){\n')

f.write('var ctx = document.getElementById("canvas").getContext("2d");\n')
f.write('window.myLine = new Chart(ctx).Line(lineChartData, {\n')
f.write('responsive: true\n')
f.write('});\n')

f.write('var ctx2 = document.getElementById("canvas2").getContext("2d");\n')
f.write('window.myLine = new Chart(ctx2).Line(lineChartData2, {\n')
f.write('responsive: true\n')
f.write('});\n')

f.write('var ctx3 = document.getElementById("canvas3").getContext("2d");\n')
f.write('window.myLine = new Chart(ctx3).Line(lineChartData3, {\n')
f.write('responsive: true\n')
f.write('});\n')

f.write('var ctx4 = document.getElementById("canvas4").getContext("2d");\n')
f.write('window.myLine = new Chart(ctx4).Line(lineChartData4, {\n')
f.write('responsive: true\n')
f.write('});\n')

f.write('}\n')
#onload

#segment
f.write('var lineChartData = {\n')
f.write('labels : '+str(map(str,axis0))+',\n')
f.write('datasets : [\n')
f.write('{\n')
f.write('label: "My First dataset 1",\n')
f.write('fillColor : "rgba(220,220,220,0.2)",\n')
f.write('strokeColor : "rgba(220,220,220,1)",\n')
f.write('pointColor : "rgba(220,220,220,1)",\n')
f.write('pointStrokeColor : "#fff",\n')
f.write('pointHighlightFill : "#fff",\n')
f.write('pointHighlightStroke : "rgba(220,220,220,1)",\n')
f.write('data : '+str(map(str,axis1))+'\n')
f.write('}\n')
f.write(']\n')
f.write('}\n')

#segment

f.write('var lineChartData2 = {\n')
f.write('labels : '+str(map(str,axis1))+',\n')
f.write('datasets : [\n')
f.write('{\n')
f.write('label: "My First dataset 2",\n')
f.write('fillColor : "rgba(220,220,220,0.2)",\n')
f.write('strokeColor : "rgba(220,220,220,1)",\n')
f.write('pointColor : "rgba(220,220,220,1)",\n')
f.write('pointStrokeColor : "#fff",\n')
f.write('pointHighlightFill : "#fff",\n')
f.write('pointHighlightStroke : "rgba(220,220,220,1)",\n')
f.write('data : '+str(map(str,axis4))+'\n')
f.write('}\n')
f.write(']\n')
f.write('}\n')

#segment
f.write('var lineChartData3 = {\n')
f.write('labels : '+str(map(str,axis1))+',\n')
f.write('datasets : [\n')
f.write('{\n')
f.write('label: "My First dataset 3",\n')
f.write('fillColor : "rgba(220,220,220,0.2)",\n')
f.write('strokeColor : "rgba(220,220,220,1)",\n')
f.write('pointColor : "rgba(220,220,220,1)",\n')
f.write('pointStrokeColor : "#fff",\n')
f.write('pointHighlightFill : "#fff",\n')
f.write('pointHighlightStroke : "rgba(220,220,220,1)",\n')
f.write('data : '+str(map(str,axis8))+'\n')
f.write('}\n')
f.write(']\n')
f.write('}\n')

#segment
f.write('var lineChartData4 = {\n')
f.write('labels : '+str(map(str,axis1))+',\n')
f.write('datasets : [\n')
f.write('{\n')
f.write('label: "My First dataset 4",\n')
f.write('fillColor : "rgba(220,220,220,0.2)",\n')
f.write('strokeColor : "rgba(220,220,220,1)",\n')
f.write('pointColor : "rgba(220,220,220,1)",\n')
f.write('pointStrokeColor : "#fff",\n')
f.write('pointHighlightFill : "#fff",\n')
f.write('pointHighlightStroke : "rgba(220,220,220,1)",\n')
f.write('data : '+str(map(str,axis9))+'\n')
f.write('}\n')
f.write(']\n')
f.write('}\n')

#segment
f.write('</script>')
f.write('</body>')
f.write('</html>')

f.close()
