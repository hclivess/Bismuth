import getpass
import math
import base64
import socket
import sys
import sqlite3
import os
import hashlib
import time
import logging

from simplecrypt import decrypt

from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA

from multiprocessing import Process
from multiprocessing import freeze_support

# logging #multiprocessing not supported for file output

app_log = logging.getLogger('root')
app_log.setLevel(logging.INFO)

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s %(funcName)s(%(lineno)d) %(message)s')
ch.setFormatter(formatter)
app_log.addHandler(ch)
# logging

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
    if "diff_recalc=" in line:
        diff_recalc_conf = line.strip('diff_recalc=')
# load config

def send(sdef, data):
    sdef.sendall(data)

def bin_convert(string):
    return ''.join(format(ord(x), 'b') for x in string)

def miner(args, password):
    # import keys
    if not os.path.exists('privkey_encrypted.der'):
        key = RSA.importKey(open('privkey.der').read())
        # private_key_readable = str(key.exportKey())
        # public_key = key.publickey()
    else:
        encrypted_privkey = open('privkey_encrypted.der').read()
        decrypted_privkey = decrypt(password, base64.b64decode(encrypted_privkey))
        key = RSA.importKey(decrypted_privkey)  # be able to sign
        # private_key_readable = decrypted_privkey

    public_key_readable = open('pubkey.der').read()
    public_key_hashed = base64.b64encode(public_key_readable)
    address = hashlib.sha224(public_key_readable).hexdigest()
    # import keys

    block_timestamp = 0  # init
    tries = 0

    while True:
        try:
            if str(block_timestamp) != str(time.time()): #in case the time has changed
                block_timestamp = str(time.time())
                tries = tries +1
                # calculate new hash

                if tries % int(diff_recalc_conf) == 0 or tries == 1:

                    conn = sqlite3.connect("static/ledger.db") #open to select the last tx to create a new hash from
                    conn.text_factory = str
                    c = conn.cursor()
                    c.execute("SELECT block_hash, block_height,timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
                    result = c.fetchall()

                    db_block_hash = result[0][0]
                    db_block_height = result[0][1]
                    timestamp_last_block = float(result[0][2])
                    #print timestamp_last_block

                    # calculate difficulty
                    c.execute("SELECT avg(timestamp) FROM transactions where block_height >= '" + str(db_block_height - 30) + "' and reward != 0;")
                    timestamp_avg = c.fetchall()[0][0]  # select the reward block
                    #print timestamp_avg

                    timestamp_difference = timestamp_last_block - timestamp_avg
                    #print timestamp_difference

                    diff = float(math.log(1e18 / timestamp_difference))
                    if db_block_height < 50:
                        diff = 4
                    #if diff < 4:
                    #    diff = 4
                    # calculate difficulty
                    conn.close()

                    app_log.info("Mining, " + str(tries) + " cycles passed in thread " + str(args) + ", difficulty: " + str(diff))
                    diff = int(diff)
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

                if bin_convert(address)[0:diff] in bin_convert(block_hash):
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

                            send(s, (str(len("block"))).zfill(10))
                            send(s, "block")
                            send(s, (str(len(str(block_send)))).zfill(10))
                            send(s, str(block_send))

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
            raise

if __name__ == '__main__':
    password = getpass.getpass()

    # verify connection
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
    # verify connection

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

    freeze_support()
    instances = range(int(mining_threads_conf))
    print instances
    for q in instances:
        p = Process(target=miner, args=(str(q+1), password))
        p.start()
        print "thread "+str(p)+ " started"
    p.join()
    p.terminate()
