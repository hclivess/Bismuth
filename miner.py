import base64
import socket
import sys
import sqlite3
import os
import hashlib
import time
import logging
from logging.handlers import RotatingFileHandler

from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA

from multiprocessing import Process

global busy
busy = 0

# load config
lines = [line.rstrip('\n') for line in open('config.txt')]
for line in lines:
    if "port=" in line:
        port = line.strip('port=')
    if "mining_ip=" in line:
        mining_ip_conf = line.strip("mining_ip=")
    if "segment_limit=" in line:
        segment_limit_conf = line.strip('segment_limit=')
    if "mining_threads=" in line:
        mining_threads_conf = line.strip('mining_threads=')
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

#verify connection
connected = 0
while connected == 0:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((mining_ip_conf, int(port)))  # connect to local node
        app_log.info("Connected")
        connected = 1
        s.close()
    except:
        app_log.info("Miner: Please start your node for the block to be submitted or adjust mining ip in settings.")
        time.sleep(1)
#verify connection

if not os.path.exists('mempool.db'):
    # create empty mempool
    mempool = sqlite3.connect('mempool.db')
    mempool.text_factory = str
    m = mempool.cursor()
    m.execute(
        "CREATE TABLE IF NOT EXISTS transactions (timestamp, address, recipient, amount, signature, public_key, openfield)")
    mempool.commit()
    mempool.close()
    app_log.info("Core: Created mempool file")
    # create empty mempool
else:
    app_log.info("Mempool exists")

def split2len(s, n):
    def _f(s, n):
        while s:
            yield s[:n]
            s = s[n:]
    return list(_f(s, n))

def miner(args):
    block_timestamp = 0  # init
    tries = 0

    while True:
        if str(block_timestamp) != str(time.time()): #in case the time has changed
            block_timestamp = str(time.time())
            app_log.info("Mining in progress, " + str(tries) + " cycles have passed in thread "+ str(args))
            tries = tries +1
            # calculate new hash


            while busy == 1:
                time.sleep(0.1)
            busy = 1
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
            busy = 0

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

                submitted = 0
                while submitted == 0:
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.connect((mining_ip_conf, int(port)))  # connect to local node
                        app_log.info("Connected")

                        app_log.info("Miner: Proceeding to submit mined block")
                        s.sendall("block______")
                        time.sleep(0.1)
                        #print block_send

                        # send own
                        miner_split = split2len(str(transactions), int(segment_limit_conf))  # ledger txs must be converted to string
                        miner_count = len(miner_split)  # how many segments of 500 will be sent
                        while len(str(miner_count)) != 10:
                            miner_count = "0" + str(miner_count)  # number must be 10 long
                        s.sendall(str(miner_count))  # send how many segments will be transferred
                        time.sleep(0.1)
                        # print (str(miner_count))

                        miner_index = -1
                        while int(miner_count) > 0:
                            miner_count = int(miner_count) - 1
                            miner_index = miner_index + 1

                            segment_length = len(miner_split[miner_index])
                            while len(str(segment_length)) != 10:
                                segment_length = "0" + str(segment_length)
                            s.sendall(
                                segment_length)  # send how much they should receive, usually 500, except the last segment
                            app_log.info("Client: Segment length: " + str(segment_length))
                            time.sleep(0.1)
                            app_log.info(
                                "Client: Segment to dispatch: " + str(miner_split[miner_index]))  # send segment !!!!!!!!!
                            s.sendall(miner_split[miner_index])  # send segment
                            time.sleep(0.1)
                        # send own
                        s.close()
                        submitted = 1

                    except:
                        app_log.info("Miner: Please start your node for the block to be submitted or adjust mining ip in settings.")
                        time.sleep(1)


                global busy
                while busy == 1:
                    time.sleep(0.1)
                busy = 1

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
                busy = 0

            #submit mined block to node
        else:
            time.sleep(0.1)
            #break

if __name__ == '__main__':
    instances = range(int(mining_threads_conf))
    print instances
    for q in instances:
        p = Process(target=miner, args=str(q+1))
        p.start()
        print "thread "+str(p)+ " started"

