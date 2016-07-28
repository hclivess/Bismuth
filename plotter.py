import matplotlib.pyplot as plt
import sqlite3

conn = sqlite3.connect('./ledger.db')
c = conn.cursor()
c.execute("SELECT * FROM transactions ORDER BY block_height ASC;")

all = c.fetchall()

xaxis0 = []
yaxis1 = []
yaxis4 = []
yaxis8 = []
yaxis9 = []



for x in all:
    xaxis0.append(x[0]) # append timestamp

    yaxis1.append(x[1]) # append block height
    yaxis4.append(x[4])  # append amount
    yaxis8.append(x[8])  # append fee
    yaxis9.append(x[9])  # append reward

plt.figure(1)                # the first figure

plt.subplot(221)
plt.yscale('log')
plt.plot([xaxis0], [yaxis1], 'ro')
plt.title('1')
plt.grid(True)

plt.subplot(222)
plt.yscale('log')
plt.plot([xaxis0], [yaxis4], 'ro')
plt.title('2')
plt.grid(True)

plt.subplot(223)
plt.yscale('log')
plt.plot([xaxis0], [yaxis8], 'ro')
plt.title('3')
plt.grid(True)

plt.subplot(224)
plt.yscale('log')
plt.plot([xaxis0], [yaxis9], 'ro')
plt.title('4')
plt.grid(True)

plt.show()