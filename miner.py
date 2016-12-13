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
from multiprocessing import freeze_support

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

#logging #multiprocessing not supported for file output

app_log = logging.getLogger('root')
app_log.setLevel(logging.INFO)

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
    except Exception, e:
        print e
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

def miner(args):
    block_timestamp = 0  # init
    tries = 0

    while True:
        try:
            if str(block_timestamp) != str(time.time()): #in case the time has changed
                block_timestamp = str(time.time())
                app_log.info("Mining in progress, " + str(tries) + " cycles have passed in thread "+ str(args))
                tries = tries +1
                # calculate new hash

                conn = sqlite3.connect("ledger.db") #open to select the last tx to create a new hash from
                conn.text_factory = str
                c = conn.cursor()
                c.execute("SELECT block_hash, block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
                result = c.fetchall()

                db_block_hash = result[0][0]
                db_block_height = result[0][1]

                # calculate difficulty
                c.execute("SELECT timestamp FROM transactions WHERE block_height = '" + str(db_block_height) + "'")
                timestamp_last_block = c.fetchall()[-1]  # select the reward block
                # print timestamp_last_block[0]

                c.execute("SELECT timestamp FROM transactions WHERE block_height = '" + str(db_block_height - 1) + "'")
                timestamp_before_last_block = c.fetchall()[-1]  # select the reward block
                # print timestamp_before_last_block[0]

                minutes_passed = (time.time() - float(timestamp_last_block[0])) / 60
                # print float(timestamp_last_block[0]) - float(timestamp_before_last_block[0])
                diff = int(5 / ((float(timestamp_last_block[0]) - float(timestamp_before_last_block[0])) / 60) - minutes_passed * 8)
                if diff < 3:
                    diff = 3
                app_log.info("Calculated difficulty: " + str(diff))
                # calculate difficulty

                conn.close()

                #serialize txs
                mempool = sqlite3.connect("mempool.db")
                mempool.text_factory = str
                m = mempool.cursor()
                m.execute("SELECT * FROM transactions ORDER BY timestamp;")
                result = m.fetchall() #select all txs from mempool
                mempool.close()

                block_send = []
                del block_send[:] # empty
                removal_signature = []
                del removal_signature[:] # empty

                for dbdata in result:
                    transaction = (dbdata[0],dbdata[1],dbdata[2],str(float(dbdata[3])),dbdata[4],dbdata[5],dbdata[6]) #create tuple
                    #print transaction
                    block_send.append(transaction) #append tuple to list for each run
                    removal_signature.append(str(dbdata[4])) #for removal after successful mining

                # claim reward
                transaction_reward = tuple
                transaction_reward = (block_timestamp,address,address,str(float(0)),"reward") #only this part is signed!
                #print transaction_reward

                h = SHA.new(str(transaction_reward))
                signer = PKCS1_v1_5.new(key)
                signature = signer.sign(h)
                signature_enc = base64.b64encode(signature)

                block_send.append((block_timestamp,address,address,str(float(0)),signature_enc,public_key_hashed,"reward"))
                # claim reward

                #print "sync this"
                #print block_timestamp
                #print block_send  # sync this
                #print db_block_hash
                #print (str((block_timestamp,block_send,db_block_hash)))
                block_hash = hashlib.sha224(str((block_timestamp,block_send,db_block_hash))).hexdigest()  # we now need to use block timestamp as a variable for hash generation !!!

                #start mining

                # serialize txs

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
                            block_send_length = str(len(str(block_send))).zfill(10)
                            s.sendall(block_send_length)

                            totalsent = 0
                            while totalsent < len(block_send):
                                sent = s.send(str(block_send)[totalsent:])
                                if sent == 0:
                                    raise RuntimeError("socket connection broken")
                                totalsent = totalsent + sent
                                # send own

                            submitted = 1

                        except Exception, e:
                            print e
                            app_log.info("Miner: Please start your node for the block to be submitted or adjust mining ip in settings.")
                            time.sleep(1)

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
                time.sleep(0.1)
                #break
        except Exception, e:
            print e
            time.sleep(0.1)
            pass

if __name__ == '__main__':
    freeze_support()
    instances = range(int(mining_threads_conf))
    print instances
    for q in instances:
        p = Process(target=miner, args=str(q+1))
        p.start()
        print "thread "+str(p)+ " started"
    p.join()
    p.terminate()


