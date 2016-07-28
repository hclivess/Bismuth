#error: for some reason produces different hashes than node, will be investigated
#single address miner with timer limited to 2 decimal places, no customization of timestamp, no multithreading, no multiple-address mining
#a part txhash must equal a part of address
import base64
import socket
import sys
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

conn=sqlite3.connect("ledger.db")
c = conn.cursor()

timestamp = 0 #init
tries = 0
inform = 1

while True:
    # decide reward

    c.execute("SELECT reward FROM transactions ORDER BY block_height DESC LIMIT 50;")  # check if there has been a reward in past 50 blocks
    was_reward = c.fetchall()

    reward_possible = 1

    for x in was_reward:
        if x[0] != "0": #iterate all of the last 50 blocks and see if there is a reward in there
            reward_possible = 0  #there has been a reward already, don't reward anymore

    if reward_possible == 0:
        if inform == 1:
            app_log.info("Mempool: Reward status: Mined for this segment already. One segment is 50 blocks long. You need to wait for those 50 blocks to pass before mining is available again. The miner will resume automatically.")
            inform = 0
        time.sleep(10)

    else:  # no reward in the past x blocks
        inform = 1
        c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 50;")  # select previous x transactions to start mining
        db_txhash_list = c.fetchall()
        app_log.info("Mempool: Reward status: Not mined")

        reward = 0

        #start mining

        while True:
            if str(timestamp) != str(time.time()): #in case the time has changed
                tries = tries +1
                # calculate new hash (submit only if mining is successful)
                c.execute("SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1;")
                result = c.fetchall()
                db_timestamp = result[0][1]
                db_address = result[0][2]
                db_to_address = result[0][3]
                db_amount = result[0][4]
                db_signature = result[0][5]
                db_txhash = result[0][7]
                db_transaction = str(db_timestamp) + ":" + str(db_address) + ":" + str(db_to_address) + ":" + str(db_amount)

                timestamp = str(time.time())
                app_log.info("Timestamp: " + timestamp)
                transaction = str(timestamp) + ":" + str(address) + ":" + str(address) + ":" + str(0.0)#send 0 token to self to collect reward

                h = SHA.new(transaction)
                signer = PKCS1_v1_5.new(key)
                signature = signer.sign(h)
                signature_enc = base64.b64encode(signature)


                txhash = hashlib.sha224(str(transaction) + str(signature_enc) + str(db_txhash)).hexdigest()  # calculate txhash from the ledger
                # calculate new hash
                app_log.info("Txhash: "+txhash)
                app_log.info("Attempt: " + str(tries))
                #start mining

                diff = 3
                if address[0:diff] == txhash[0:diff]:
                    app_log.info("Miner: Found a good txhash in "+str(tries)+" attempts")
                    tries = 0

                    #submit mined block to node
                    app_log.info("Miner: Encoded Signature: " + str(signature_enc))

                    verifier = PKCS1_v1_5.new(key)
                    if verifier.verify(h, signature) == True:
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.connect(("127.0.0.1", int("2829")))  # connect to local node
                        app_log.info("Connected")

                        app_log.info("Miner: Proceeding to submit mined block")
                        s.sendall("transaction")
                        time.sleep(0.1)
                        transaction_send = (transaction + ";" + str(signature_enc) + ";" + public_key_readable)

                        # announce length
                        txhash_len = len(str(transaction_send))
                        while len(str(txhash_len)) != 10:
                            txhash_len = "0" + str(txhash_len)
                        app_log.info("Miner: Announcing " + str(txhash_len) + " length of transaction")
                        s.sendall(str(txhash_len))
                        time.sleep(0.1)
                        # announce length

                        s.sendall(transaction_send)
                        time.sleep(0.1)
                        s.close()
                    #submit mined block to node

                else:
                    app_log.info("Miner: Txhash not matching reward conditions")
                    break
            # decide reward

conn.close()
