import matplotlib.pyplot as plt
import sqlite3

conn = sqlite3.connect('./ledger.db')
c = conn.cursor()
c.execute("SELECT * FROM transactions ORDER BY block_height ASC;")

all = c.fetchall()

axis0 = []
axisn = []

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


plt.figure(1)                # the first figure

plt.subplot(221)
plt.yscale('log')
plt.plot([axis1], [axis0], 'ro')
plt.title('Blocks per time')
plt.grid(True)

plt.subplot(222)
plt.yscale('log')
plt.plot([axis1], [axis4], 'ro')
plt.title('Amount per block height')
plt.grid(True)

plt.subplot(223)
plt.yscale('log')
plt.plot([axis1], [axis8], 'ro')
plt.title('Fee per block height')
plt.grid(True)

plt.subplot(224)
plt.yscale('log')
plt.plot([axis1], [axis9], 'ro')
plt.title('Reward per block height')
plt.grid(True)

plt.show()