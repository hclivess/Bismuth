import sqlite3
import hashlib
import time
import logging
from logging.handlers import RotatingFileHandler

from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA

#import keys
key = RSA.importKey(open('privkey.der').read())
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
address = hashlib.sha224(public_key_readable).hexdigest()
#import keys

#logging
log_formatter = logging.Formatter('%(asctime)s %(levelname)s %(funcName)s(%(lineno)d) %(message)s')
logFile = 'miner.log'
my_handler = RotatingFileHandler(logFile, mode='a', maxBytes=5*1024*1024, backupCount=2, encoding=None, delay=0)
my_handler.setFormatter(log_formatter)
my_handler.setLevel(logging.INFO)
app_log = logging.getLogger('root')
app_log.setLevel(logging.INFO)
app_log.addHandler(my_handler)

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s %(funcName)s(%(lineno)d) %(message)s')
ch.setFormatter(formatter)
app_log.addHandler(ch)
#logging

timestamp = str(time.time())
transaction = str(timestamp) + ":" + str(address) + ":" + str(address) + ":" + 1

conn=sqlite3.connect("ledger.db")
c = conn.cursor()

c.execute("SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1;")
result = c.fetchall()
db_txhash = result[0][7]
db_block_height = result[0][0]
block_height_new = db_block_height + 1
db_timestamp_last = result[0][1]  # for fee calc

try:
    c.execute("SELECT reward FROM transactions ORDER BY block_height DESC LIMIT 50;")  # check if there has been a reward in past 50 blocks
    was_reward = c.fetchone()[0]  # no error if there has been a reward
    reward = 0  # no error? there has been a reward already, don't reward anymore
except:  # no reward in the past x blocks
    if address[0:5] == db_txhash[0:5]:
        reward = 50
        app_log.info("Mempool: Heureka, reward mined: " + str(reward))
    else:
        reward = 0
        app_log.info("Mempool: Too bad, reward has already been given out for this interval")
        # decide reward

conn.close()