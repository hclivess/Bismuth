import sys
import sqlite3
import hashlib
import time
import logging
from logging.handlers import RotatingFileHandler

from Crypto.PublicKey import RSA

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

conn=sqlite3.connect("ledger.db")
c = conn.cursor()

while True:
    # decide reward
    try:
        c.execute("SELECT block_height FROM transactions WHERE reward !='0' ORDER BY block_height DESC LIMIT 50;")  # check if there has been a reward in past 50 blocks
        was_reward = c.fetchone()[0]  # no error if there has been a reward
        reward = 0  # no error? there has been a reward already, don't reward anymore
        app_log.info("Mempool: Reward status: Mined (" + str(was_reward) + ")")
        time.sleep(10)

    except:  # no reward in the past x blocks
        c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 50;")  # select previous x transactions to start mining
        db_txhash_list = c.fetchall()
        app_log.info("Mempool: Reward status: Not mined")

        reward = 0

        #start mining
        while True:
            # calculate new hash (submit only if mining is successful)
            c.execute("SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1;")
            result = c.fetchall()
            #db_block_height = result[0][0]
            db_timestamp = result[0][1]
            db_address = result[0][2]
            db_to_address = result[0][3]
            db_amount = result[0][4]
            db_signature = result[0][5]
            db_txhash = result[0][7]
            db_transaction = str(db_timestamp) + ":" + str(db_address) + ":" + str(db_to_address) + ":" + str(db_amount)

            timestamp = str(time.time())
            transaction = str(timestamp) + ":" + str(address) + ":" + str(address) + ":" + str(1)

            txhash = hashlib.sha224(str(transaction) + str(db_signature) + str(db_txhash)).hexdigest()  # calculate txhash from the ledger
            # calculate new hash
            app_log.info("Txhash:"+txhash)
            #start mining

            if address[0:5] == txhash[0:5]:
                reward = 50
                app_log.info("Mempool: Found a good txhash")
                #todo: submit here

            if reward == 0:
                app_log.info("Mempool: Txhash not matching reward conditions")
                break
        # decide reward

conn.close()