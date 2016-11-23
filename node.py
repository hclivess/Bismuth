# caution: fees are not redistributed at the moment
import SocketServer
import ast
import base64
import gc
import hashlib
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import logging
from logging.handlers import RotatingFileHandler

from Crypto import Random
from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5

# load config
lines = [line.rstrip('\n') for line in open('config.txt')]
for line in lines:
    if "port=" in line:
        port = line.strip('port=')
    if "genesis=" in line:
        genesis_conf = line.strip('genesis=')
    if "verify=" in line:
        verify_conf = line.strip('verify=')
    if "version=" in line:
        version_conf = line.strip('version=')
    if "thread_limit=" in line:
        thread_limit_conf = line.strip('thread_limit=')
    if "rebuild_db=" in line:
        rebuild_db_conf = line.strip('rebuild_db=')
    if "debug=" in line:
        debug_conf = line.strip('debug=')
    if "purge=" in line:
        purge_conf = line.strip('purge=')

# load config

version = version_conf

def most_common(lst):
    return max(set(lst), key=lst.count)


gc.enable()

log_formatter = logging.Formatter('%(asctime)s %(levelname)s %(funcName)s(%(lineno)d) %(message)s')
logFile = 'node.log'
my_handler = RotatingFileHandler(logFile, mode='a', maxBytes=5 * 1024 * 1024, backupCount=2, encoding=None, delay=0)
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

global active_pool
active_pool = []
global consensus_ip_list
consensus_ip_list = []
global consensus_blockheight_list
consensus_blockheight_list = []
global consensus_hash_list
consensus_hash_list = []
global tried
tried = []
global mempool_busy
mempool_busy = 0
global consensus_percentage
consensus_percentage = 100
global banlist
banlist = []
global busy
busy = 0

#port = 2829 now defined by config

def merge_mempool(data):
    # merge mempool
    transaction_list = ast.literal_eval(data)
    for transaction in transaction_list:
        mempool_timestamp = transaction[0]
        mempool_address = transaction[1]
        mempool_recipient = transaction[2]
        mempool_amount = transaction[3]
        mempool_signature_enc = transaction[4]
        mempool_public_key_readable = transaction[5]
        mempool_openfield = transaction[6]

        mempool = sqlite3.connect('mempool.db')
        mempool.text_factory = str
        m = mempool.cursor()

        m.execute("SELECT * FROM transactions WHERE signature = '"+mempool_signature_enc+"'")

        try:
            result = m.fetchall()[0]
            app_log.info("That transaction is already in our mempool")

        except:
            m.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?)", (mempool_timestamp,mempool_address,mempool_recipient,str(float(mempool_amount)),mempool_signature_enc,mempool_public_key_readable,mempool_openfield))
            app_log.info("Node: Mempool updated with a received transaction")
            mempool.commit()  # Save (commit) the changes
            mempool.close()


            # merge mempool

            # receive mempool

def purge_old_peers():
    with open("peers.txt", "r") as peer_list:
        peers = peer_list.read()
        peer_tuples = re.findall("'([\d\.]+)', '([\d]+)'", peers)
        # app_log.info(peer_tuples)

        for tuple in peer_tuples:
            HOST = tuple[0]
            # app_log.info(HOST)
            PORT = int(tuple[1])
            # app_log.info(PORT)

            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect((HOST, PORT))
                s.close()
            except:
                if purge_conf == 1:
                    # remove from peerlist if not connectible
                    peer_tuples.remove((HOST, str(PORT)))
                    app_log.info("Removed formerly active peer " + str(HOST) + " " + str(PORT))
                pass

            output = open("peers.txt", 'w')
            for x in peer_tuples:
                output.write(str(x) + "\n")
            output.close()

def verify():
    try:
        invalid = 0
        for row in c.execute('SELECT * FROM transactions ORDER BY block_height'):
            db_block_height = row[0]
            db_timestamp = row[1]
            db_address = row[2]
            db_recipient = row[3]
            db_amount = row[4]
            db_signature_enc = row[5]
            db_public_key_readable = row[6]
            db_public_key = RSA.importKey(db_public_key_readable)
            db_openfield = row[11]

            db_transaction = (db_timestamp,db_address,db_recipient,str(float(db_amount)),db_openfield)

            db_signature_dec = base64.b64decode(db_signature_enc)
            verifier = PKCS1_v1_5.new(db_public_key)
            h = SHA.new(str(db_transaction))
            if verifier.verify(h, db_signature_dec):
                pass
            else:
                app_log.info("The following transaction is invalid:")
                app_log.info(row)
                invalid = invalid + 1
                if db_block_height == str(1):
                    app_log.info("Core: Your genesis signature is invalid, someone meddled with the database")
                    sys.exit(1)

        if invalid == 0:
            app_log.info("Core: All transacitons in the local ledger are valid")

    except sqlite3.Error, e:
        app_log.info("Core: Error %s:" % e.args[0])
        sys.exit(1)
    finally:
        if conn:
            conn.close()
            # verify blockchain

def blocknotfound(block_hash_delete):
    global busy
    if busy == 1:
        app_log.info("Skipping")
    else:
        try:
            busy = 1

            conn = sqlite3.connect('ledger.db')
            conn.text_factory = str
            c = conn.cursor()

            c.execute('SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1')
            results = c.fetchone()
            db_block_height = results[0]
            db_timestamp = results[1]
            #db_address = results[2]
            #db_recipient = results[3]
            #db_amount = results[4]
            #db_signature = results[5]
            #db_public_key_readable = results[6]
            db_block_hash = results[7]
            db_confirmations = results[10]

            if db_block_height < 2:
                app_log.info("Client: Will not roll back this block")
                conn.close()

            elif (db_confirmations > 30) and (time.time() < (float(db_timestamp) + 120)):  # unstuck after x seconds
                app_log.info("Client: Too many confirmations for rollback and the block is too fresh")
                conn.close()

            elif (db_block_hash != block_hash_delete):
                #print db_block_hash
                #print block_hash_delete
                app_log.info("Client: We moved away from the block to rollback, skipping")
                conn.close()

            else:
                app_log.info("Client: Node didn't find the block, deleting latest entry")

                # delete followups
                c.execute('DELETE FROM transactions WHERE block_height >="' + str(db_block_height) + '"')
                conn.commit()
                conn.close()
        except:
            pass
        busy = 0

            # delete followups

def consensus_add(consensus_ip, consensus_blockheight, consensus_hash):
    global consensus_ip_list
    global consensus_blockheight_list
    global consensus_hash_list
    global consensus_percentage

    if consensus_ip not in consensus_ip_list:
        app_log.info("Adding " + str(consensus_ip) + " to consensus peer list")
        consensus_ip_list.append(consensus_ip)
        app_log.info("Assigning " + str(consensus_blockheight) + " to peer block height list")
        consensus_blockheight_list.append(str(int(consensus_blockheight)))
        app_log.info("Assigning " + str(consensus_hash) + " to peer hash list")
        consensus_hash_list.append(str(consensus_hash))

    if consensus_ip in consensus_ip_list:
        consensus_index = consensus_ip_list.index(consensus_ip)  # get where in this list it is

        if consensus_blockheight_list[consensus_index] == (consensus_blockheight):
            app_log.info("Opinion of " + str(consensus_ip) + " hasn't changed")

        else:
            del consensus_ip_list[consensus_index]  # remove ip
            del consensus_blockheight_list[consensus_index]  # remove ip's opinion
            if consensus_blockheight != "none":
                del consensus_hash_list[consensus_index] # remove hash

            app_log.info("Updating " + str(consensus_ip) + " in consensus")
            consensus_ip_list.append(consensus_ip)
            consensus_blockheight_list.append(int(consensus_blockheight))
            if consensus_blockheight != "none":
                consensus_hash_list.append(str(consensus_hash))

    app_log.info("Consensus IP list:" + str(consensus_ip_list))
    app_log.info("Consensus opinion list:" + str(consensus_blockheight_list))
    app_log.info("Consensus hash list:" + str(consensus_hash_list))

    consensus = most_common(consensus_blockheight_list)

    consensus_percentage = (float(consensus_blockheight_list.count(consensus) / float(len(consensus_blockheight_list)))) * 100
    app_log.info("Current active connections: " + str(len(active_pool)))
    app_log.info("Current block consensus: " + str(consensus) + " = " + str(consensus_percentage) + "%")

    return

def consensus_remove(consensus_ip):
    global consensus_ip_list
    global consensus_blockheight_list
    if consensus_ip in consensus_ip_list:
        app_log.info(
            "Will remove " + str(consensus_ip) + " from consensus pool " + str(consensus_ip_list))
        consensus_index = consensus_ip_list.index(consensus_ip)
        consensus_ip_list.remove(consensus_ip)
        del consensus_blockheight_list[consensus_index]  # remove ip's opinion
        del consensus_hash_list[consensus_index]  # remove hash
    else:
        app_log.info("Client " + str(consensus_ip) + " not present in the consensus pool")

def manager():
    global banlist
    while True:
        with open("peers.txt", "r") as peer_list:
            peers = peer_list.read()
            peer_tuples = re.findall("'([\d\.]+)', '([\d]+)'", peers)
            # app_log.info(peer_tuples)

            threads_count = threading.active_count()
            threads_limit = thread_limit_conf

            for tuple in peer_tuples:
                HOST = tuple[0]
                # app_log.info(HOST)
                PORT = int(tuple[1])
                # app_log.info(PORT)

                app_log.info(HOST + ":" + str(PORT))
                if threads_count <= threads_limit and str(HOST + ":" + str(PORT)) not in tried and str(
                                        HOST + ":" + str(PORT)) not in active_pool and str(HOST) not in banlist:
                    tried.append(HOST + ":" + str(PORT))
                    t = threading.Thread(target=worker, args=(HOST, PORT))  # threaded connectivity to nodes here
                    app_log.info("---Starting a client thread " + str(threading.currentThread()) + "---")
                    t.start()

                    # client thread handling
        if len(active_pool) < 3:
            app_log.info("Only " + str(len(active_pool)) + " connections active, resetting the try list")
            del tried[:]

        app_log.info("Connection manager: Threads at " + str(threads_count) + "/" + str(threads_limit))
        app_log.info("Tried: " + str(tried))
        app_log.info("Current active pool: " + str(active_pool))
        app_log.info("Current connections: " + str(len(active_pool)))

        # app_log.info(threading.enumerate() all threads)
        time.sleep(10)
    return

def digest_block(data):  # this function has become the transaction engine core over time, rudimentary naming
    global busy
    if busy == 1:
        app_log.info("Skipping")
    else:
        busy = 1
        while True:
            try:
                signature_valid = 0
                conn = sqlite3.connect('ledger.db')
                conn.text_factory = str
                c = conn.cursor()

                app_log.info("Node: Digesting incoming block: " + data)

                transaction_list = ast.literal_eval(data)
                #print "!!! is this valid?"
                #print transaction_list

                for transaction in transaction_list:
                    # verify signatures
                    received_timestamp = transaction[0]
                    received_address = transaction[1]
                    received_recipient = transaction[2]
                    received_amount = str(float(transaction[3]))
                    received_signature_enc = transaction[4]
                    received_public_key_readable = transaction[5]
                    received_openfield = transaction[6]

                    received_public_key = RSA.importKey(received_public_key_readable) #convert readable key to instance
                    received_signature_dec = base64.b64decode(received_signature_enc)
                    verifier = PKCS1_v1_5.new(received_public_key)

                    h = SHA.new(str((received_timestamp, received_address, received_recipient, received_amount,received_openfield)))

                    if verifier.verify(h, received_signature_dec):
                        app_log.info("Node: The signature is valid")

                    if transaction == transaction_list[-1]:  # recognize the last transaction as the mining reward transaction
                        miner_address = received_address
                        block_timestamp = received_timestamp


                            # verify signatures


                c.execute("SELECT block_height,block_hash FROM transactions ORDER BY block_height DESC LIMIT 1;")  # extract data from ledger to construct new block_hash
                result = c.fetchall()
                db_block_height = result[0][0]
                db_block_hash = result[0][1]
                block_height_new = db_block_height + 1

                for transaction in transaction_list:
                    db_timestamp = transaction[0]
                    db_address = transaction[1]
                    db_recipient = transaction[2]
                    db_amount = transaction[3]
                    db_signature = transaction[4]
                    db_public_key_readable = transaction[5]
                    db_openfield = transaction[6]

                    print "sync this"
                    #print block_timestamp
                    #print transaction_list
                    #print db_block_hash
                    print (str((block_timestamp,transaction_list,db_block_hash)))
                    block_hash = hashlib.sha224(str((block_timestamp,transaction_list,db_block_hash))).hexdigest()  # calculate block_hash from the ledger #PROBLEM HEREEEEE

                    app_log.info("Mempool: tx sig not found in the local ledger, proceeding to check before insert")
                    # if not in ledger
                    # calculate block height from the ledger

                    # verify balance
                    conn = sqlite3.connect('ledger.db')
                    conn.text_factory = str
                    c = conn.cursor()

                    app_log.info("Mempool: Verifying balance")
                    app_log.info("Mempool: Received address: " + str(received_address))
                    c.execute("SELECT sum(amount) FROM transactions WHERE recipient = '" + received_address + "'")
                    credit = c.fetchone()[0]
                    c.execute("SELECT sum(amount) FROM transactions WHERE address = '" + received_address + "'")
                    debit = c.fetchone()[0]
                    c.execute("SELECT sum(fee) FROM transactions WHERE address = '" + received_address + "'")
                    fees = c.fetchone()[0]
                    c.execute("SELECT sum(reward) FROM transactions WHERE address = '" + db_address + "'")
                    rewards = c.fetchone()[0]
                    if debit == None:
                        debit = 0
                    if fees == None:
                        fees = 0
                    if rewards == None:
                        rewards = 0
                    if credit == None:
                        credit = 0
                    app_log.info("Mempool: Total credit: " + str(credit))
                    app_log.info("Mempool: Total debit: " + str(debit))
                    balance = float(credit) - float(debit) - float(fees) + float(rewards)
                    app_log.info("Mempool: Transction address balance: " + str(balance))

                    db_block_50 = int(db_block_height) - 50
                    try:
                        c.execute("SELECT timestamp FROM transactions WHERE block_height ='" + str(db_block_50) + "';")
                        db_timestamp_50 = c.fetchone()[0]
                        fee = abs(1000 / (float(db_timestamp) - float(db_timestamp_50)))
                        app_log.info("Fee: " + str(fee))

                    except Exception as e:
                        fee = 1  # presumably there are less than 50 txs
                        app_log.info("Fee error: " + str(e))
                        # raise #debug
                    # calculate fee

                    # decide reward

                    diff = 3

                    time_now = str(time.time())
                    if float(time_now) < float(db_timestamp):
                        app_log.info("Mempool: Future mining not allowed")

                    else:
                        if transaction == transaction_list[-1]:
                            reward = 25
                            fee = 0
                        else:
                            reward = 0
                          # dont request a fee for mined block so new accounts can mine

                        if miner_address[0:diff] == block_hash[0:diff]:  # simplified comparison, no backwards mining
                            app_log.info("Mempool: Difficulty requirement satisfied")

                            if (float(balance))-(float(fee)+float(db_amount)) < 0:
                                app_log.info("Mempool: Cannot afford to pay fees") #NEED TO MOVE THIS WHOLE TO MEMPOOL VALIDATION INSTEAD

                            else:
                                c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (block_height_new,db_timestamp,db_address,db_recipient,str(float(db_amount)),db_signature,db_public_key_readable,block_hash,fee,reward,str(0),db_openfield))
                                conn.commit()
                            conn.close()

                        else:
                            app_log.info("Mempool: Difficulty requirement not satisfied: "+miner_address+" "+block_hash)

                    mempool = sqlite3.connect('mempool.db')
                    mempool.text_factory = str
                    m = mempool.cursor()
                    try:
                        m.execute(
                            "DELETE FROM transactions WHERE signature = '" + db_signature + "';")  # delete tx from mempool now that it is in the ledger or if it was a double spend
                        mempool.commit()
                    except:
                        #tx was not in the local mempool
                        pass
                    mempool.close()

            except:
                app_log.info("Digesting complete")
                if debug_conf == 1:
                    raise#debug
            busy = 0
            return


def db_maintenance():
    # db maintenance
    conn = sqlite3.connect("ledger.db")
    conn.execute("VACUUM")
    conn.close()
    conn = sqlite3.connect("mempool.db")
    conn.execute("VACUUM")
    conn.close()
    app_log.info("Core: Database maintenance finished")


# key maintenance
if os.path.isfile("privkey.der") is True:
    app_log.info("Client: privkey.der found")
else:
    # generate key pair and an address
    random_generator = Random.new().read
    key = RSA.generate(1024, random_generator)
    public_key = key.publickey()

    private_key_readable = str(key.exportKey())
    public_key_readable = str(key.publickey().exportKey())
    address = hashlib.sha224(public_key_readable).hexdigest()  # hashed public key
    # generate key pair and an address

    app_log.info("Client: Your address: " + str(address))
    app_log.info("Client: Your private key: " + str(private_key_readable))
    app_log.info("Client: Your public key: " + str(public_key_readable))

    pem_file = open("privkey.der", 'a')
    pem_file.write(str(private_key_readable))
    pem_file.close()

    pem_file = open("pubkey.der", 'a')
    pem_file.write(str(public_key_readable))
    pem_file.close()

    address_file = open("address.txt", 'a')
    address_file.write(str(address) + "\n")
    address_file.close()

# import keys
key = RSA.importKey(open('privkey.der').read())
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
address = hashlib.sha224(public_key_readable).hexdigest()

app_log.info("Client: Local address: " + str(address))

if not os.path.exists('mempool.db'):
    # create empty mempool
    mempool = sqlite3.connect('mempool.db')
    mempool.text_factory = str
    m = mempool.cursor()
    m.execute("CREATE TABLE IF NOT EXISTS transactions (timestamp, address, recipient, amount, signature, public_key, openfield)")
    mempool.commit()
    mempool.close()
    app_log.info("Core: Created mempool file")
    # create empty mempool
else:
    app_log.info("Mempool exists")

if rebuild_db_conf == 1:
    db_maintenance()
# connectivity to self node

# verify blockchain
con = None
conn = sqlite3.connect('ledger.db')
conn.text_factory = str
c = conn.cursor()
# c.execute("CREATE TABLE IF NOT EXISTS transactions (block_height, address, recipient, amount, signature, public_key)")
c.execute("SELECT Count(*) FROM transactions")
db_rows = c.fetchone()[0]
app_log.info("Core: Total steps: " + str(db_rows))

# verify genesis
c.execute("SELECT recipient FROM transactions ORDER BY block_height ASC LIMIT 1")
genesis = c.fetchone()[0]
app_log.info("Core: Genesis: " + genesis)
if str(
        genesis) != genesis_conf:  # change this line to your genesis address if you want to clone
    app_log.info("Core: Invalid genesis address")
    sys.exit(1)
# verify genesis
if verify_conf == 1:
    verify()

### LOCAL CHECKS FINISHED ###

class ThreadedTCPRequestHandler(SocketServer.BaseRequestHandler):
    def handle(self):  # server defined here
        while True:
            try:
                data = self.request.recv(11)
                # cur_thread = threading.current_thread()
                app_log.info("Node: Received: " + data + " from " + str(
                    self.request.getpeername()[0]))  # will add custom ports later

                if data == 'version____':
                    data = self.request.recv(11)
                    if version != data:
                        app_log.info("Protocol version mismatch: " + data +", should be "+version)
                        self.request.sendall("notok______")
                        time.sleep(0.1)
                        raise
                    else:
                        app_log.info("Node: Protocol version matched: " + data)
                        self.request.sendall("ok_________")
                        time.sleep(0.1)

                if data == 'mempool____':
                    #receive theirs
                    data = self.request.recv(10)
                    app_log.info("Node: Mempool length to receive: " + data)
                    mempool_len = int(data)
                    data = self.request.recv(mempool_len)
                    app_log.info("Node: " + data)
                    merge_mempool(data)
                    #receive theirs

                    mempool = sqlite3.connect('mempool.db')
                    mempool.text_factory = str
                    m = mempool.cursor()
                    m.execute('SELECT * FROM transactions')
                    mempool_txs = m.fetchall()

                    #remove transactions which are already in the ledger
                    conn = sqlite3.connect('ledger.db')
                    conn.text_factory = str
                    c = conn.cursor()
                    for transaction in mempool_txs:
                        try:
                            c.execute("SELECT signature FROM transactions WHERE signature = '" + transaction[4] + "';")  # try and select the mempool tx in ledger
                            ledger_sig = c.fetchall()[0][0]
                            m.execute("DELETE FROM transactions WHERE signature = '" + ledger_sig + "';") #and delete it
                            app_log.info("A transaction has been deleted from mempool because it already is in the ledger")
                        except:
                            pass
                    conn.close()
                    mempool.close()
                    #remove transactions which are already in the ledger

                    app_log.info("Extracted from the mempool: "+str(mempool_txs)) #improve: sync based on signatures only

                    mempool_len = len(str(mempool_txs))
                    while len(str(mempool_len)) != 10:
                        mempool_len = "0" + str(mempool_len)
                    app_log.info("Announcing " + str(mempool_len) + " length of mempool")
                    self.request.sendall(str(mempool_len))
                    time.sleep(0.1)

                    self.request.sendall(str(mempool_txs))
                    time.sleep(0.1)

                if data == 'helloserver':
                    with open("peers.txt", "r") as peer_list:
                        peers = peer_list.read()
                        app_log.info("Node: " + peers)
                        self.request.sendall("peers______")
                        time.sleep(0.1)
                        self.request.sendall(peers)
                        time.sleep(0.1)
                    peer_list.close()

                    # save peer if connectible
                    peer_file = open("peers.txt", 'r')
                    peer_tuples = []
                    for line in peer_file:
                        extension = re.findall("'([\d\.]+)', '([\d]+)'", line)
                        peer_tuples.extend(extension)
                    peer_file.close()
                    peer_ip = str(self.request.getpeername()[0])
                    peer_tuple = ("('" + peer_ip + "', '" + str(port) + "')")

                    try:
                        app_log.info("Testing connectivity to: " + str(peer_ip))
                        peer_test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        peer_test.connect((str(peer_ip), int(str(port))))  # double parentheses mean tuple
                        app_log.info("Node: Distant peer connectible")
                        if peer_tuple not in str(peer_tuples):  # stringing tuple is a nasty way
                            peer_list_file = open("peers.txt", 'a')
                            peer_list_file.write((peer_tuple) + "\n")
                            app_log.info("Node: Distant peer saved to peer list")
                            peer_list_file.close()
                        else:
                            app_log.info("Core: Distant peer already in peer list")
                    except:
                        app_log.info("Node: Distant peer not connectible")

                        # raise #test only

                    # save peer if connectible

                    while busy == 1:
                        time.sleep(1)
                    app_log.info("Node: Sending sync request")
                    self.request.sendall("sync_______")
                    time.sleep(0.1)

                if data == "sendsync___":
                    while busy == 1:
                        time.sleep(1)
                    self.request.sendall("sync_______")
                    time.sleep(0.1)

                if data == "blockfound_":
                    app_log.info("Node: Client has the block")  # node should start sending txs in this step

                    data = self.request.recv(10)
                    app_log.info("Transaction length to receive: " + data)
                    block_hash_len = int(data)
                    data = self.request.recv(block_hash_len)

                    #app_log.info("Client: " + data)

                    digest_block(data)
                    #digest_block() #temporary

                    while busy == 1:
                        time.sleep(1)
                    self.request.sendall("sync_______")
                    time.sleep(0.1)

                if data == "blockheight":
                    subdata = self.request.recv(11)  # receive client's last block height
                    received_block_height = subdata
                    app_log.info("Node: Received block height: " + received_block_height)

                    # consensus pool 1 (connection from them)
                    consensus_ip = self.request.getpeername()[0]
                    consensus_blockheight = int(subdata)  # str int to remove leading zeros
                    consensus_add(consensus_ip,consensus_blockheight,"none")
                    # consensus pool 1 (connection from them)

                    conn = sqlite3.connect('ledger.db')
                    conn.text_factory = str
                    c = conn.cursor()
                    c.execute('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1')
                    db_block_height = c.fetchone()[0]
                    conn.close()

                    # append zeroes to get static length
                    while len(str(db_block_height)) != 11:
                        db_block_height = "0" + str(db_block_height)
                    self.request.sendall(db_block_height)
                    time.sleep(0.1)
                    # send own block height

                    if received_block_height > db_block_height:
                        app_log.info("Node: Client has higher block")
                        update_me = 1

                    if received_block_height < db_block_height:
                        app_log.info("Node: We have a higher block, hash will be verified")
                        update_me = 0

                    if received_block_height == db_block_height:
                        app_log.info("Node: We have the same block height, hash will be verified")
                        update_me = 0

                    if update_me == 1:

                        conn = sqlite3.connect('ledger.db')
                        conn.text_factory = str
                        c = conn.cursor()
                        c.execute('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1')
                        db_block_hash = c.fetchone()[0]  # get latest block_hash
                        conn.close()

                        app_log.info("Node: block_hash to send: " + str(db_block_hash))
                        self.request.sendall(db_block_hash)  # send latest block_hash
                        time.sleep(0.1)

                        #receive their latest hash
                        #confirm you know that hash or continue receiving

                    if update_me == 0:  # update them if update_me is 0

                        data = self.request.recv(56)  # receive client's last block_hash
                        # send all our followup hashes

                        app_log.info("Node: Will seek the following block: " + str(data))

                        conn = sqlite3.connect('ledger.db')
                        conn.text_factory = str
                        c = conn.cursor()

                        try:
                            c.execute("SELECT block_height FROM transactions WHERE block_hash='" + data + "'")
                            block_hash_client_block = c.fetchone()[0]

                            app_log.info("Node: Client is at block " + str(
                                block_hash_client_block))  # now check if we have any newer

                            c.execute('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1')
                            db_block_hash = c.fetchone()[0]  # get latest block_hash
                            if db_block_hash == data:
                                app_log.info("Node: Client has the latest block")
                                self.request.sendall("nonewblocks")
                                time.sleep(0.1)

                            else:
                                c.execute("SELECT timestamp,address,recipient,amount,signature,public_key,openfield FROM transactions WHERE block_height='" + str(
                                    int(block_hash_client_block) + 1) + "'")  # select incoming transaction + 1, only columns that need not be verified
                                block_hash_send = c.fetchall()

                                app_log.info("Node: Selected " + str(block_hash_send) + " to send")

                                conn.close()
                                self.request.sendall("blockfound_")
                                time.sleep(0.1)

                                block_hash_len = len(str(block_hash_send))
                                while len(str(block_hash_len)) != 10:
                                    block_hash_len = "0" + str(block_hash_len)
                                app_log.info("Announcing " + str(block_hash_len) + " length of block")
                                self.request.sendall(str(block_hash_len))
                                time.sleep(0.1)

                                self.request.sendall(str(block_hash_send))
                                time.sleep(0.1)

                        except:
                            app_log.info("Node: Block not found")
                            self.request.sendall("blocknotfou")
                            time.sleep(0.1)
                            self.request.sendall(data)
                            time.sleep(0.1)
                            # newly apply on self
                            conn = sqlite3.connect('ledger.db')
                            conn.text_factory = str
                            c = conn.cursor()
                            c.execute('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1')
                            db_block_hash = c.fetchone()[0]  # get latest block_hash
                            conn.close()
                            blocknotfound(db_block_hash)
                            # newly apply on self

                if data == "nonewblocks":
                    #digest_block() #temporary #otherwise passive node will not be able to digest

                    self.request.sendall("sync_______")
                    time.sleep(0.1)

                if data == "blocknotfou":
                    block_hash_delete = self.request.recv(56)
                    blocknotfound(block_hash_delete)

                    while busy == 1:
                        time.sleep(1)
                    app_log.info("Client: Deletion complete, sending sync request")
                    self.request.sendall("sync_______")
                    time.sleep(0.1)

                if data == "block______":
                    data = self.request.recv(10)
                    app_log.info("Block length to receive: " + data)
                    block_len = int(data)
                    data = self.request.recv(block_len)

                    digest_block(data)

                if data == "":
                    app_log.info("Node: Communication error")
                    raise
                time.sleep(0.1)
                # app_log.info("Server resting") #prevent cpu overload
            except Exception, e:
                app_log.info("Node: Lost connection")
                app_log.info(e)

                # remove from consensus (connection from them)
                consensus_ip = self.request.getpeername()[0]
                consensus_remove(consensus_ip)
                # remove from consensus (connection from them)

                raise #for test purposes
                break


# client thread
def worker(HOST, PORT):
    while True:
        try:
            connected = 0
            this_client = (HOST + ":" + str(PORT))
            this_client_ip = HOST
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            #s.settimeout(25)
            s.connect((HOST, PORT))
            app_log.info("Client: Connected to " + str(HOST) + " " + str(PORT))
            connected = 1
            if this_client not in active_pool:
                active_pool.append(this_client)
                app_log.info("Current active pool: " + str(active_pool))

            first_run = 1
            while True:
                # communication starter
                if first_run == 1:
                    first_run = 0

                    s.sendall('version____')
                    time.sleep(0.1)
                    s.sendall(version)
                    time.sleep(0.1)
                    data = s.recv(11)
                    if data == "ok_________":
                        app_log.info("Client: Node protocol version matches our client")
                    else:
                        app_log.info("Client: Node protocol version mismatch")
                        raise

                    s.sendall('helloserver')
                    time.sleep(0.1)

                # communication starter
                data = s.recv(11)  # receive data, one and the only root point
                app_log.info('Client: Received '+data+' from ' + this_client)
                if data == "":
                    app_log.info("Communication error")
                    raise

                if data == "peers______":
                    subdata = s.recv(2048)  # peers are larger
                    # get remote peers into tuples
                    server_peer_tuples = re.findall("'([\d\.]+)', '([\d]+)'", subdata)
                    app_log.info(server_peer_tuples)
                    app_log.info(len(server_peer_tuples))
                    # get remote peers into tuples

                    # get local peers into tuples
                    peer_file = open("peers.txt", 'r')
                    peer_tuples = []
                    for line in peer_file:
                        extension = re.findall("'([\d\.]+)', '([\d]+)'", line)
                        peer_tuples.extend(extension)
                    peer_file.close()
                    app_log.info(peer_tuples)
                    # get local peers into tuples

                    for x in server_peer_tuples:
                        if x not in peer_tuples:
                            app_log.info("Client: " + str(x) + " is a new peer, saving if connectible")
                            try:
                                s_purge = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                s_purge.connect((HOST[x], PORT[x]))  # save a new peer file with only active nodes
                                s_purge.close()

                                peer_list_file = open("peers.txt", 'a')
                                peer_list_file.write(str(x) + "\n")
                                peer_list_file.close()
                            except:
                                app_log.info("Not connectible")

                        else:
                            app_log.info("Client: " + str(x) + " is not a new peer")

                if data == "sync_______":
                    # sync start

                    # send block height, receive block height
                    s.sendall("blockheight")
                    time.sleep(0.1)

                    conn = sqlite3.connect('ledger.db')
                    conn.text_factory = str
                    c = conn.cursor()
                    c.execute('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1')
                    db_block_height = c.fetchone()[0]
                    conn.close()

                    app_log.info("Client: Sending block height to compare: " + str(db_block_height))
                    # append zeroes to get static length
                    while len(str(db_block_height)) != 11:
                        db_block_height = "0" + str(db_block_height)
                    s.sendall(str(db_block_height))
                    time.sleep(0.1)

                    subdata = s.recv(11)  # receive node's block height
                    received_block_height = subdata
                    app_log.info("Client: Node is at block height: " + str(received_block_height))

                    # todo add to active pool here?

                    if received_block_height < db_block_height:
                        app_log.info("Client: We have a higher, sending")
                        update_me = 0

                    if received_block_height > db_block_height:
                        app_log.info("Client: Node has higher block, receiving")
                        update_me = 1

                    if received_block_height == db_block_height:
                        app_log.info("Client: We have the same block height, hash will be verified")
                        update_me = 1

                    if update_me == 1:

                        conn = sqlite3.connect('ledger.db')
                        conn.text_factory = str
                        c = conn.cursor()
                        c.execute('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1')
                        db_block_hash = c.fetchone()[0]  # get latest block_hash
                        conn.close()

                        app_log.info("Client: block_hash to send: " + str(db_block_hash))
                        s.sendall(db_block_hash)  # send latest block_hash
                        time.sleep(0.1)

                        # consensus pool 2 (active connection)
                        consensus_ip = s.getpeername()[0]
                        consensus_blockheight = int(subdata)  # str int to remove leading zeros
                        consensus_add(consensus_ip, consensus_blockheight, "none")
                        # consensus pool 2 (active connection)

                        #receive their latest hash
                        #confirm you know that hash or continue receiving

                    if update_me == 0:  # update them if update_me is 0
                        data = s.recv(56)  # receive client's last block_hash

                        # send all our followup hashes
                        app_log.info("Client: Will seek the following block: " + str(data))

                        # consensus pool 2 (active connection)
                        consensus_ip = s.getpeername()[0]
                        consensus_blockheight = int(subdata)  # str int to remove leading zeros
                        consensus_add(consensus_ip, consensus_blockheight, data)
                        # consensus pool 2 (active connection)

                        conn = sqlite3.connect('ledger.db')
                        conn.text_factory = str
                        c = conn.cursor()

                        try:
                            c.execute("SELECT block_height FROM transactions WHERE block_hash='" + data + "'")
                            block_hash_client_block = c.fetchone()[0]

                            app_log.info("Client: Node is at block " + str(
                                block_hash_client_block))  # now check if we have any newer

                            c.execute('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1')
                            db_block_hash = c.fetchone()[0]  # get latest block_hash
                            if db_block_hash == data:
                                app_log.info("Client: Node has the latest block")
                                s.sendall("nonewblocks")
                                time.sleep(0.1)

                            else:
                                c.execute("SELECT timestamp,address,recipient,amount,signature,public_key,openfield FROM transactions WHERE block_height='" + str(
                                    int(block_hash_client_block) + 1) + "'")  # select incoming transaction + 1
                                block_hash_send = c.fetchall()

                                app_log.info("Client: Selected " + str(block_hash_send) + " to send")

                                conn.close()
                                s.sendall("blockfound_")
                                time.sleep(0.1)

                                block_hash_len = len(str(block_hash_send))
                                while len(str(block_hash_len)) != 10:
                                    block_hash_len = "0" + str(block_hash_len)
                                app_log.info("Announcing " + str(block_hash_len) + " length of block")
                                s.sendall(str(block_hash_len))
                                time.sleep(0.1)

                                s.sendall(str(block_hash_send))
                                time.sleep(0.1)

                        except:
                            app_log.info("Node: Block not found")
                            s.sendall("blocknotfou")
                            time.sleep(0.1)
                            # newly apply on self
                            conn = sqlite3.connect('ledger.db')
                            conn.text_factory = str
                            c = conn.cursor()
                            c.execute('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1')
                            db_block_hash = c.fetchone()[0]  # get latest block_hash
                            conn.close()
                            blocknotfound(db_block_hash)
                            # newly apply on self

                if data == "blocknotfou":
                    block_hash_delete = s.recv(56)
                    blocknotfound(block_hash_delete)

                    while busy == 1:
                        time.sleep(1)
                    s.sendall("sendsync___")
                    time.sleep(0.1)

                if data == "blockfound_":

                    app_log.info("Client: Node has the block")  # node should start sending txs in this step

                    data = s.recv(10)
                    app_log.info("Transaction length to receive: " + data)
                    block_hash_len = int(data)
                    data = s.recv(block_hash_len)
                    app_log.info("Client: " + data)

                    digest_block(data)
                    #digest_block() #temporary

                    while busy == 1:
                        time.sleep(1)
                    s.sendall("sendsync___")
                    time.sleep(0.1)

                        # block_hash validation end

                if data == "nonewblocks":
                    #digest_block() #temporary #otherwise passive node will not be able to digest

                    # sand and receive mempool
                    s.sendall("mempool____")

                    #send own
                    mempool = sqlite3.connect('mempool.db')
                    mempool.text_factory = str
                    m = mempool.cursor()
                    m.execute('SELECT * FROM transactions')
                    mempool_txs = m.fetchall()

                    # remove transactions which are already in the ledger
                    conn = sqlite3.connect('ledger.db')
                    conn.text_factory = str
                    c = conn.cursor()
                    for transaction in mempool_txs:
                        try:
                            c.execute("SELECT signature FROM transactions WHERE signature = '" + transaction[
                                4] + "';")  # try and select the mempool tx in ledger
                            ledger_sig = c.fetchall()[0][0]
                            m.execute(
                                "DELETE FROM transactions WHERE signature = '" + ledger_sig + "';")  # and delete it
                            app_log.info(
                                "A transaction has been deleted from mempool because it already is in the ledger")
                        except:
                            pass
                    conn.close()
                    mempool.close()
                    # remove transactions which are already in the ledger

                    app_log.info(
                        "Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    mempool_len = len(str(mempool_txs))
                    while len(str(mempool_len)) != 10:
                        mempool_len = "0" + str(mempool_len)
                    app_log.info("Announcing " + str(mempool_len) + " length of mempool")
                    s.sendall(str(mempool_len))
                    time.sleep(0.1)

                    s.sendall(str(mempool_txs))
                    time.sleep(0.1)
                    #send own


                    time.sleep(0.1)
                    data = s.recv(10)
                    app_log.info("Client: Mempool length to receive: " + data)
                    mempool_len = int(data)
                    data = s.recv(mempool_len)
                    app_log.info("Client: " + data)
                    merge_mempool(data)
                    # receive mempool

                    app_log.info("Client: We seem to be at the latest block. Paused before recheck")

                    time.sleep(10)
                    while busy == 1:
                        time.sleep(1)
                    s.sendall("sendsync___")
                    time.sleep(0.1)

        except Exception as e:
            #remove from active pool
            if this_client in active_pool:
                app_log.info("Will remove " + str(this_client) + " from active pool " + str(active_pool))
                active_pool.remove(this_client)
            # remove from active pool

            # remove from consensus 2
            if connected == 1:#if ever connected
                consensus_ip = s.getpeername()[0]
                consensus_remove(consensus_ip)
            # remove from consensus 2

            app_log.info("Connection to " + this_client + " terminated due to " + str(e))
            app_log.info("---thread " + str(threading.currentThread()) + " ended---")
            if debug_conf == 1:
                raise  # debug
            return

    return


class ThreadedTCPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    pass


if __name__ == "__main__":
    try:
        # Port 0 means to select an arbitrary unused port
        HOST, PORT = "0.0.0.0", int(port)

        server = ThreadedTCPServer((HOST, PORT), ThreadedTCPRequestHandler)
        ip, port = server.server_address

        purge_old_peers()

        # Start a thread with the server -- that thread will then start one
        # more thread for each request

        server_thread = threading.Thread(target=server.serve_forever)

        # Exit the server thread when the main thread terminates

        server_thread.daemon = True
        server_thread.start()
        app_log.info("Server loop running in thread: " + server_thread.name)

        # start connection manager
        t_manager = threading.Thread(target=manager())
        app_log.info("Starting connection manager")
        t_manager.start()
        # start connection manager

        # server.serve_forever() #added
        server.shutdown()
        server.server_close()

    except Exception, e:
        app_log.info("Node already running?")
        app_log.info(e)
        raise  # only test
sys.exit()
