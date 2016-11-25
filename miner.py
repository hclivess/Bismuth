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

# load config
lines = [line.rstrip('\n') for line in open('config.txt')]
for line in lines:
    if "port=" in line:
        port = line.strip('port=')
    if "mining_ip=" in line:
        mining_ip_conf = line.strip("mining_ip=")

# load config

#import keys
key = RSA.importKey(open('privkey.der').read())
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
public_key_hashed = base64.b64encode(public_key_readable)
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

block_timestamp = 0 #init
tries = 0
inform = 1

app_log.info("Mining will start once there are transactions in the mempool")
while True:
    mempool = sqlite3.connect("mempool.db")
    mempool.text_factory = str
    m = mempool.cursor()
    m.execute("SELECT * FROM transactions ORDER BY timestamp;")
    result = m.fetchall()
    mempool.close()

    while True:

        if str(block_timestamp) != str(time.time()) and result: #in case the time has changed
            block_timestamp = str(time.time())
            app_log.info("Mining in progress, " + str(tries) + " cycles have passed")
            tries = tries +1
            # calculate new hash

            conn = sqlite3.connect("ledger.db") #open to select the last tx to create a new hash from
            conn.text_factory = str
            c = conn.cursor()
            c.execute("SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1;")
            result = c.fetchall()
            conn.close()

            db_block_hash = result[0][0]

            #serialize txs
            mempool = sqlite3.connect("mempool.db")
            mempool.text_factory = str
            m = mempool.cursor()
            m.execute("SELECT * FROM transactions ORDER BY timestamp;")
            result = m.fetchall() #select all txs from mempool
            mempool.close()

            if result:
                transactions = []
                del transactions[:] # empty
                removal_signature = []
                del removal_signature[:] # empty

                for dbdata in result:
                    transaction = (dbdata[0],dbdata[1],dbdata[2],str(float(dbdata[3])),dbdata[4],dbdata[5],dbdata[6]) #create tuple
                    #print transaction
                    transactions.append(transaction) #append tuple to list for each run
                    removal_signature.append(str(dbdata[4])) #for removal after successful mining

                # claim reward
                transaction_reward = tuple
                transaction_reward = (block_timestamp,address,address,str(float(0)),"reward") #only this part is signed!
                #print transaction_reward

                h = SHA.new(str(transaction_reward))
                signer = PKCS1_v1_5.new(key)
                signature = signer.sign(h)
                signature_enc = base64.b64encode(signature)

                transactions.append((block_timestamp,address,address,str(float(0)),signature_enc,public_key_hashed,"reward"))
                # claim reward

                #print "sync this"
                #print block_timestamp
                #print transactions  # sync this
                #print db_block_hash
                #print (str((block_timestamp,transactions,db_block_hash)))
                block_hash = hashlib.sha224(str((block_timestamp,transactions,db_block_hash))).hexdigest()  # we now need to use block timestamp as a variable for hash generation !!!

                #start mining

                # serialize txs

                diff = 3
                if address[0:diff] == block_hash[0:diff]:
                    app_log.info("Miner: Found a good block_hash in "+str(tries)+" cycles")
                    tries = 0

                    #submit mined block to node

                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.connect((mining_ip_conf, int(port)))  # connect to local node
                    app_log.info("Connected")

                    app_log.info("Miner: Proceeding to submit mined block")
                    s.sendall("block______")
                    time.sleep(0.1)
                    #print block_send

                    # announce length
                    block_len = len(str(transactions))
                    while len(str(block_len)) != 10:
                        block_len = "0" + str(block_len)
                    app_log.info("Miner: Announcing " + str(block_len) + " length of block")
                    s.sendall(str(block_len))
                    time.sleep(0.1)
                    # announce length

                    s.sendall(str(transactions))
                    time.sleep(0.1)
                    s.close()

                    #remove sent from mempool
                    mempool = sqlite3.connect("mempool.db")
                    mempool.text_factory = str
                    m = mempool.cursor()
                    for x in removal_signature:
                        m.execute("DELETE FROM transactions WHERE signature ='" + x + "';")
                        app_log.info("Removed a transaction with the following signature from mempool: "+str(x))
                    mempool.commit()
                    mempool.close()
                    #remove sent from mempool

                #submit mined block to node
            else:
                app_log.info("Mempool empty")
        else:
            time.sleep(0.1)
            break
