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

global sync_in_progress

def most_common(lst):
    return max(set(lst), key=lst.count)

gc.enable()

log_formatter = logging.Formatter('%(asctime)s %(levelname)s %(funcName)s(%(lineno)d) %(message)s')
logFile = 'node.log'
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

global active_pool
active_pool = []
global consensus_ip_list
consensus_ip_list = []
global consensus_opinion_list
consensus_opinion_list = []
global tried
tried = []

port = 2829

def manager():
    while True:
        with open ("peers.txt", "r") as peer_list:
            peers=peer_list.read()
            peer_tuples = re.findall ("'([\d\.]+)', '([\d]+)'",peers)
            #app_log.info(peer_tuples)

            threads_count = threading.active_count()
            threads_limit = 25

            for tuple in peer_tuples:
                HOST = tuple[0]
                #app_log.info(HOST)
                PORT = int(tuple[1])
                #app_log.info(PORT)

                app_log.info(HOST+":"+str(PORT))
                if threads_count <= threads_limit and str(HOST+":"+str(PORT)) not in tried and str(HOST+":"+str(PORT)) not in active_pool:
                    tried.append(HOST+":"+str(PORT))
                    t = threading.Thread(target=worker, args=(HOST,PORT))#threaded connectivity to nodes here
                    app_log.info("---Starting a client thread "+str(threading.currentThread())+"---")
                    t.start()

            #client thread handling
        if len(active_pool) < 3:
            app_log.info("Only " + str(len(active_pool)) + " connections active, resetting the try list")
            del tried[:]

        app_log.info("Connection manager: Threads at " + str(threads_count) + "/" + str(threads_limit))
        app_log.info("Tried: " + str(tried))
        app_log.info("Current active pool: " + str(active_pool))
        #app_log.info(threading.enumerate() all threads)
        time.sleep(10)
    return

def restore_backup():
    while True:
        try:
            app_log.info("Node: Digesting backup")

            backup = sqlite3.connect('backup.db')
            b = backup.cursor()

            b.execute("SELECT * FROM transactions ORDER BY timestamp ASC LIMIT 1;")
            result = b.fetchall()
            db_timestamp = result[0][0]
            db_address = result[0][1]
            db_to_address = result[0][2]
            db_amount = result[0][3]
            db_signature_enc = result[0][4]
            db_public_key_readable = result[0][5]

            # insert to mempool
            mempool = sqlite3.connect('mempool.db')
            m = mempool.cursor()

            m.execute("INSERT INTO transactions VALUES ('" + str(db_timestamp) + "','" + str(db_address) + "','" + str(db_to_address) + "','" + str(db_amount) + "','" + str(db_signature_enc) + "','" + str(db_public_key_readable) + "')")  # Insert a row of data
            app_log.info("Node: Mempool updated with a transaction from backup")
            mempool.commit()  # Save (commit) the changes
            mempool.close()

            app_log.info("Backup: deleting digested tx")
            b.execute("DELETE FROM transactions WHERE signature ='" + db_signature_enc + "';")
            backup.commit()
            backup.close()
            # insert to mempool

        except:
            digest_mempool()
            app_log.info("Backup empty, sync finished")
            global sync_in_progress
            sync_in_progress = 0
            return

def digest_mempool(): #this function has become the transaction engine core over time, rudimentary naming
    #digest mempool start
    while True:
        try:
            app_log.info("Node: Digesting mempool")

            mempool = sqlite3.connect('mempool.db')
            m = mempool.cursor()
            conn = sqlite3.connect('ledger.db')
            c = conn.cursor()

            #select
            m.execute("SELECT * FROM transactions ORDER BY timestamp ASC LIMIT 1;") #select tx from mempool to insert
            result = m.fetchall()
            db_timestamp = result[0][0]
            db_address = result[0][1]
            db_to_address = result[0][2]
            db_amount = result[0][3]
            db_signature = result[0][4]
            db_public_key_readable = result[0][5]
            db_transaction = str(db_timestamp) + ":" + str(db_address) + ":" + str(db_to_address) + ":" + str(db_amount)


            try:
                c.execute("SELECT * FROM transactions WHERE signature ='" + db_signature + "';")
                fetch_test = c.fetchone()[0]

                #if previous passes
                app_log.info("Mempool: tx already in the ledger, deleting")

                m.execute("DELETE FROM transactions WHERE signature ='" + db_signature + "';")
                mempool.commit()

            except:
                app_log.info("Mempool: tx sig not found in the local ledger, proceeding to check before insert")
                # if not in ledger
                # calculate block height from the ledger

                #verifying timestamp
                time_now = str(time.time())
                tolerance = 10
                if float(db_timestamp) > (float(time_now) + float(tolerance)):
                    app_log.info("Mempool: Timestamp is too far in the future, deleting tx")
                    m.execute("DELETE FROM transactions WHERE signature ='" + db_signature + "';")
                    mempool.commit()
                # verifying timestamp

                #verify balance
                app_log.info("Mempool: Verifying balance")
                app_log.info("Mempool: Received address: " + str(db_address))
                c.execute("SELECT sum(amount) FROM transactions WHERE to_address = '" + db_address + "'")
                credit = c.fetchone()[0]
                c.execute("SELECT sum(amount) FROM transactions WHERE address = '" + db_address + "'")
                debit = c.fetchone()[0]
                if debit == None:
                    debit = 0
                if credit == None:
                    credit = 0
                app_log.info("Mempool: Total credit: " + str(credit))
                app_log.info("Mempool: Total debit: " + str(debit))
                balance = int(credit) - int(debit)
                app_log.info("Mempool: Transction address balance: " + str(balance))

                if int(balance) - int(db_amount) < 0:
                    app_log.info("Mempool: Their balance is too low for this transaction, possible double spend attack, deleting tx")
                    m.execute("DELETE FROM transactions WHERE signature ='" + db_signature + "';")
                    mempool.commit()

                elif int(db_amount) < 0:
                    app_log.info("Mempool: Cannot use negative amounts, deleting tx")
                    m.execute("DELETE FROM transactions WHERE signature ='" + db_signature + "';")
                    mempool.commit()

                #verify balance

                else:
                    c.execute("SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1;")
                    result = c.fetchall()
                    db_txhash = result[0][7]
                    db_block_height = result[0][0]
                    block_height_new = db_block_height + 1
                    db_timestamp_last =result[0][1] #for fee calc

                    # calculate fee
                    db_block_50 = int(db_block_height)-50
                    print db_block_50
                    try:
                        c.execute("SELECT timestamp FROM transactions WHERE block_height ='" + str(db_block_50) + "';")
                        db_timestamp_50 = c.fetchone()[0]
                        fee = 1/(float(db_timestamp_last) - float(db_timestamp_50))
                        app_log.info("Fee: "+str(fee))

                    except Exception as e:
                        fee = 1 #presumably there are less than 50 txs
                        app_log.info("Fee error: "+str(e))
                        #raise #debug
                        #todo: should fees be verified or calculated every time?
                    # calculate fee

                    # decide reward
                    reward = 0
                    # decide reward

                    txhash = hashlib.sha224(str(db_transaction) + str(db_signature) + str(db_txhash)).hexdigest()  # calculate txhash from the ledger

                    c.execute("INSERT INTO transactions VALUES ('"+str(block_height_new)+"','"+str(db_timestamp)+"','"+str(db_address)+"','"+str(db_to_address)+"','"+str(db_amount)+"','"+str(db_signature)+"','" + str(db_public_key_readable) + "','" + str(txhash) + "','" + str(fee) + "','" + str(reward) + "')") # Insert a row of data
                    conn.commit()
                    conn.close()

                m.execute("DELETE FROM transactions WHERE signature = '"+db_signature+"';") #delete tx from mempool now that it is in the ledger or if it was a double spend
                mempool.commit()
                mempool.close()

        except:
            app_log.info("Mempool empty")
            #raise #debug
            return

def db_maintenance():
    #db maintenance
    conn=sqlite3.connect("ledger.db")
    conn.execute("VACUUM")
    conn.close()
    conn=sqlite3.connect("mempool.db")
    conn.execute("VACUUM")
    conn.close()
    conn=sqlite3.connect("backup.db")
    conn.execute("VACUUM")
    conn.close()
    app_log.info("Core: Database maintenance finished")

#key maintenance
if os.path.isfile("privkey.der") is True:
            app_log.info("Client: privkey.der found")
else:
    #generate key pair and an address
    random_generator = Random.new().read
    key = RSA.generate(1024, random_generator)
    public_key = key.publickey()

    private_key_readable = str(key.exportKey())
    public_key_readable = str(key.publickey().exportKey())
    address = hashlib.sha224(public_key_readable).hexdigest() #hashed public key
    #generate key pair and an address

    app_log.info("Client: Your address: "+ str(address))
    app_log.info("Client: Your private key: "+ str(private_key_readable))
    app_log.info("Client: Your public key: "+ str(public_key_readable))

    pem_file = open("privkey.der", 'a')
    pem_file.write(str(private_key_readable))
    pem_file.close()

    pem_file = open("pubkey.der", 'a')
    pem_file.write(str(public_key_readable))
    pem_file.close()

    address_file = open ("address.txt", 'a')
    address_file.write(str(address)+"\n")
    address_file.close()

# import keys
key = RSA.importKey(open('privkey.der').read())
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
address = hashlib.sha224(public_key_readable).hexdigest()

app_log.info("Client: Local address: "+ str(address))

if not os.path.exists('backup.db') == True:
    # create empty backup
    backup = sqlite3.connect('backup.db')
    b = backup.cursor()
    b.execute("CREATE TABLE IF NOT EXISTS transactions (timestamp, address, to_address, amount, signature, public_key)")
    backup.commit()
    backup.close()
    app_log.info("Core: Created backup file")
    #create empty backup
else:
    app_log.info("Backup db exists")

if not os.path.exists('mempool.db') == True:
    # create empty mempool
    mempool = sqlite3.connect('mempool.db')
    m = mempool.cursor()
    m.execute("CREATE TABLE IF NOT EXISTS transactions (timestamp, address, to_address, amount, signature, public_key)")
    mempool.commit()
    mempool.close()
    app_log.info("Core: Created mempool file")
    #create empty mempool
else:
    app_log.info("Mempool exists")

db_maintenance()
#connectivity to self node

#verify blockchain
con = None
conn = sqlite3.connect('ledger.db')
c = conn.cursor()
#c.execute("CREATE TABLE IF NOT EXISTS transactions (block_height, address, to_address, amount, signature, public_key)")
c.execute("SELECT Count(*) FROM transactions")
db_rows = c.fetchone()[0]
app_log.info("Core: Total steps: "+str(db_rows))




#verify genesis
c.execute("SELECT to_address FROM transactions ORDER BY block_height ASC LIMIT 1")
genesis = c.fetchone()[0]
app_log.info("Core: Genesis: "+genesis)
if str(genesis) != "07fb3a0e702f0eec167f1fd7ad094dcb8bdd398c91999d59e4dcb475": #change this line to your genesis address if you want to clone
    app_log.info("Core: Invalid genesis address")
    sys.exit(1)
#verify genesis

try:
    for row in c.execute('SELECT * FROM transactions ORDER BY block_height'):
        db_block_height = row[0]
        db_timestamp = row[1]
        db_address = row[2]
        db_to_address = row[3]
        db_amount = row [4]
        db_signature_enc = row[5]
        db_public_key = RSA.importKey(row[6])
        db_txhash = row[7]
        db_transaction = str(db_timestamp) +":"+ str(db_address) +":"+ str(db_to_address) +":"+ str(db_amount)

        #app_log.info(db_transaction)

        invalid = 0

        db_signature_dec = base64.b64decode(db_signature_enc)
        verifier = PKCS1_v1_5.new(db_public_key)
        h = SHA.new(db_transaction)
        if verifier.verify(h, db_signature_dec) == True:
            pass
        else:
            invalid = invalid + 1
            if db_block_height == str(1):
                app_log.info("Core: Your genesis signature is invalid, someone meddled with the database")
                sys.exit(1)

    if invalid > 0:
        app_log.info("Core: "+str(invalid)+" of the transactions in the local ledger are invalid")

    if invalid == 0:
        app_log.info("Core: All transacitons in the local ledger are valid")

except sqlite3.Error, e:
    app_log.info("Core: Error %s:" % e.args[0])
    sys.exit(1)
finally:
    if conn:
        conn.close()
#verify blockchain

### LOCAL CHECKS FINISHED ###

class ThreadedTCPRequestHandler(SocketServer.BaseRequestHandler):

    def handle(self): #server defined here
        while True:
            try:
                data = self.request.recv(11)
                #cur_thread = threading.current_thread()

                app_log.info("Node: Received: "+data+ " from " +str(self.request.getpeername()[0])) #will add custom ports later

                if data == 'helloserver':
                    with open ("peers.txt", "r") as peer_list:
                        peers=peer_list.read()
                        app_log.info("Node: "+peers)
                        self.request.sendall("peers______")
                        time.sleep(0.1)
                        self.request.sendall(peers)
                        time.sleep(0.1)
                    peer_list.close()

                    # save peer if connectible
                    peer_file = open("peers.txt", 'r')
                    peer_tuples = []
                    for line in peer_file:
                        extension = re.findall ("'([\d\.]+)', '([\d]+)'",line)
                        peer_tuples.extend(extension)
                    peer_file.close()
                    peer_ip = str(self.request.getpeername()[0])
                    peer_tuple = ("('"+peer_ip+"', '"+str(port)+"')")

                    try:
                        app_log.info("Testing connectivity to: "+str(peer_ip))
                        peer_test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        peer_test.connect((str(peer_ip), 2829)) #double parentheses mean tuple
                        app_log.info("Node: Distant peer connectible")
                        if peer_tuple not in str(peer_tuples): #stringing tuple is a nasty way
                            peer_list_file = open("peers.txt", 'a')
                            peer_list_file.write((peer_tuple)+"\n")
                            app_log.info("Node: Distant peer saved to peer list")
                            peer_list_file.close()
                        else:
                            app_log.info("Core: Distant peer already in peer list")
                    except:
                        app_log.info("Node: Distant peer not connectible")
                        #raise #test only

                    #save peer if connectible

                    app_log.info("Node: Sending sync request")
                    self.request.sendall("sync_______")
                    time.sleep(0.1)

                if data == "mytxhash___":
                    data = self.request.recv(56)  # receive client's last txhash

                    # send all our followup hashes
                    app_log.info("Node: Will seek the following block: " + str(data))
                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()

                    try:
                        c.execute("SELECT * FROM transactions WHERE txhash='" + data + "'")
                        txhash_client_block = c.fetchone()[0]

                        app_log.info("Node: Client is at block " + str(txhash_client_block))  # now check if we have any newer

                        c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                        db_txhash = c.fetchone()[0]  # get latest txhash
                        if db_txhash == data:
                            app_log.info("Client: Node has the latest block")
                            self.request.sendall("nonewblocks")
                            time.sleep(0.1)

                        else:
                            c.execute("SELECT * FROM transactions WHERE block_height='" + str(int(txhash_client_block) + 1) + "'")  # select incoming transaction + 1
                            txhash_send = c.fetchone()
                            app_log.info("Node: Selected " + str(txhash_send) + " to send")

                            conn.close()
                            self.request.sendall("blockfound_")
                            time.sleep(0.1)

                            txhash_len = len(str(txhash_send))
                            while len(str(txhash_len)) != 10:
                                txhash_len = "0" + str(txhash_len)
                            app_log.info("Announcing " + str(txhash_len) + " length of transaction")
                            self.request.sendall(str(txhash_len))
                            time.sleep(0.1)

                            self.request.sendall(str(txhash_send))
                            time.sleep(0.1)

                    except:
                        app_log.info("Client: Block not found")
                        self.request.sendall("blocknotfou")
                        time.sleep(0.1)


                if data == "sendsync___":
                    self.request.sendall("sync_______")
                    time.sleep(0.1)

                if data == "blockfound_":
                    app_log.info("Client: Node has the block") #node should start sending txs in this step

                    data = self.request.recv(10)
                    app_log.info("Transaction length to receive: "+data)
                    txhash_len = int(data)
                    data = self.request.recv(txhash_len)

                    app_log.info("Client: "+data)
                    #verify
                    sync_list = ast.literal_eval(data) #this is great, need to add it to client -> node sync
                    received_block_height = sync_list[0]
                    received_timestamp = sync_list[1]
                    received_address = sync_list[2]
                    received_to_address = sync_list[3]
                    received_amount = sync_list [4]
                    received_signature_enc = sync_list[5]
                    received_public_key_readable = sync_list[6]
                    received_public_key = RSA.importKey(sync_list[6])
                    #received_txhash = sync_list[7]
                    received_transaction = str(received_timestamp) +":"+ str(received_address) +":"+ str(received_to_address) +":"+ str(received_amount) #todo: why not have bare list instead of converting?

                    #txhash validation start

                    #open dbs for backup and followup deletion
                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()
                    c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
                    txhash_db = c.fetchone()[0]

                    #backup all followups
                    backup = sqlite3.connect('backup.db')
                    b = backup.cursor()

                    for row in c.execute('SELECT * FROM transactions WHERE block_height > "'+str(received_block_height)+'"'):
                        db_block_height = row[0]
                        db_timestamp = row[1]
                        db_address = row[2]
                        db_to_address = row[3]
                        db_amount = row [4]
                        db_signature = row[5]
                        db_public_key_readable = row[6]


                        b.execute("INSERT INTO transactions VALUES ('"+str(db_timestamp)+"','"+str(db_address)+"','"+str(db_to_address)+"','"+str(db_amount)+"','"+str(db_signature)+"','"+str(db_public_key_readable) + "')") # Insert a row of data

                    backup.commit()
                    backup.close()
                    #backup all followups

                    #delete all local followups
                    c.execute('DELETE FROM transactions WHERE block_height > "'+str(received_block_height)+'"')
                    conn.close()
                    #delete all local followups

                    app_log.info("Node: Last db txhash: "+str(txhash_db))
                    #app_log.info("Node: Received txhash: "+str(received_txhash))
                    app_log.info("Node: Received transaction: "+str(received_transaction))

                    received_signature_dec = base64.b64decode(received_signature_enc)
                    verifier = PKCS1_v1_5.new(received_public_key)
                    h = SHA.new(received_transaction)

                    if verifier.verify(h, received_signature_dec) == True:
                        app_log.info("Node: The signature is valid")
                        # transaction processing

                        # insert to mempool
                        mempool = sqlite3.connect('mempool.db')
                        m = mempool.cursor()

                        m.execute("INSERT INTO transactions VALUES ('"+str(received_timestamp)+"','"+str(received_address)+"','"+str(received_to_address)+"','"+str(received_amount)+"','"+str(received_signature_enc)+"','"+str(received_public_key_readable) + "')") # Insert a row of data
                        app_log.info("Node: Mempool updated with a received transaction")
                        mempool.commit() # Save (commit) the changes
                        mempool.close()

                        digest_mempool()
                        # insert to mempool

                        app_log.info("Node: Sending sync request")
                        self.request.sendall("sync_______")
                        time.sleep(0.1)

                    else:
                        app_log.info("Node: Signature invalid")
                        #todo consequences


                if data == "blockheight":
                    subdata = self.request.recv(11) #receive client's last block height
                    received_block_height = subdata
                    app_log.info("Node: Received block height: "+(received_block_height))

                    # consensus pool
                    consensus_ip = self.request.getpeername()[0]
                    consensus_opinion = subdata

                    if consensus_ip not in consensus_ip_list:
                        app_log.info("Adding " + str(consensus_ip) + " to consensus peer list")
                        consensus_ip_list.append(consensus_ip)
                        app_log.info("Assigning " + str(consensus_opinion) + " to peer's opinion list")
                        consensus_opinion_list.append(str(int(consensus_opinion)))

                    if consensus_ip in consensus_ip_list:
                        consensus_index = consensus_ip_list.index(consensus_ip)  # get where in this list it is
                        if consensus_opinion_list[consensus_index] == (consensus_opinion):
                            app_log.info("Opinion of " + str(consensus_ip) + " hasn't changed")

                        else:
                            del consensus_ip_list[consensus_index]  # remove ip
                            del consensus_opinion_list[consensus_index]  # remove ip's opinion
                            app_log.info("Updating " + str(consensus_ip) + " in consensus")
                            consensus_ip_list.append(consensus_ip)
                            consensus_opinion_list.append(int(consensus_opinion))

                    app_log.info("Consensus IP list:" + str(consensus_ip_list))
                    app_log.info("Consensus opinion list:" + str(consensus_opinion_list))

                    consensus = most_common(consensus_opinion_list)
                    consensus_percentage = float(consensus_opinion_list.count(float(consensus)) / float(len(consensus_opinion_list))) * 100
                    app_log.info("Current active connections: " + str(len(active_pool)))
                    app_log.info("Current block consensus: " + str(consensus) + " = " + str(consensus_percentage) + "%")
                    # consensus pool

                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()
                    c.execute('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1')
                    db_block_height = c.fetchone()[0]
                    conn.close()

                    #append zeroes to get static length
                    while len(str(db_block_height)) != 11:
                        db_block_height = "0"+str(db_block_height)
                    self.request.sendall(db_block_height)
                    time.sleep(0.1)
                    #send own block height

                    if received_block_height > db_block_height:
                        app_log.info("Node: Client has higher block, checking consensus deviation")
                        if int(received_block_height) - consensus <= 500:
                            app_log.info("Node: Deviation within normal")
                            update_me = 1
                        else:
                            app_log.info("Suspiciously high deviation, disconnecting")
                            return


                    if received_block_height < db_block_height:
                        app_log.info("Node: We have a higher block, hash will be verified")
                        update_me = 0

                    if received_block_height == db_block_height:
                        app_log.info("Node: We have the same block height, hash will be verified")
                        update_me = 0

                    if update_me == 1:
                        global sync_in_progress
                        sync_in_progress = 1

                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()
                        c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                        db_txhash = c.fetchone()[0] #get latest txhash
                        conn.close()
                        app_log.info("Node: txhash to send: " +str(db_txhash))
                        self.request.sendall(db_txhash) #send latest txhash
                        time.sleep(0.1)

                    if update_me == 0: #update them if update_me is 0
                        data = self.request.recv(56) #receive client's last txhash

                        #send all our followup hashes
                        app_log.info("Node: Will seek the following block: " + str(data))
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()

                        try:
                            c.execute("SELECT * FROM transactions WHERE txhash='" + data + "'")
                            txhash_client_block = c.fetchone()[0]

                            app_log.info("Node: Client is at block "+str(txhash_client_block)) #now check if we have any newer

                            c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                            db_txhash = c.fetchone()[0] #get latest txhash
                            if db_txhash == data:
                                app_log.info("Node: Client has the latest block")
                                self.request.sendall("nonewblocks")
                                time.sleep(0.1)

                            else:
                                c.execute("SELECT * FROM transactions WHERE block_height='"+str(int(txhash_client_block) + 1)+"'") #select incoming transaction + 1
                                txhash_send = c.fetchone()

                                app_log.info("Node: Selected "+str(txhash_send)+" to send")

                                conn.close()
                                self.request.sendall("blockfound_")
                                time.sleep(0.1)

                                txhash_len = len(str(txhash_send))
                                while len(str(txhash_len)) != 10:
                                    txhash_len = "0" + str(txhash_len)
                                app_log.info("Announcing "+str(txhash_len)+ " length of transaction")
                                self.request.sendall(str(txhash_len))
                                time.sleep(0.1)

                                self.request.sendall(str(txhash_send))
                                time.sleep(0.1)

                        except:
                            app_log.info("Node: Block not found")
                            self.request.sendall("blocknotfou")
                            time.sleep(0.1)

                if data == "blocknotfou":
                    app_log.info("Client: Node didn't find the block, deleting latest entry")
                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()
                    c.execute('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1')


                    #backup all followups to backup
                    backup = sqlite3.connect('backup.db')
                    b = backup.cursor()

                    c.execute('SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1')
                    results = c.fetchone()
                    db_timestamp = results[1]
                    db_address = results[2]
                    db_to_address = results[3]
                    db_amount = results[4]
                    db_signature = results[5]
                    db_public_key_readable = results[6]

                    b.execute("INSERT INTO transactions VALUES ('"+str(db_timestamp)+"','"+str(db_address)+"','"+str(db_to_address)+"','"+str(db_amount)+"','"+str(db_signature)+"','"+str(db_public_key_readable) + "')") # Insert a row of data

                    backup.commit()
                    backup.close()
                    #backup all followups to backup

                    #delete followups
                    c.execute('DELETE FROM transactions WHERE block_height ="'+str(db_block_height)+'"')
                    conn.commit()
                    conn.close()
                    #delete followups
                    app_log.info("Client: Deletion complete, sending sync request")
                    self.request.sendall("sync_______")
                    time.sleep(0.1)


                #latest local block
                if data == "transaction":
                    data = self.request.recv(2048)
                    data_split = data.split(";")
                    received_transaction = data_split[0]
                    app_log.info("Node: Received transaction: "+received_transaction)
                    #split message into values
                    try:
                        received_transaction_split = received_transaction.split(":")#todo receive list
                        received_timestamp = received_transaction_split[0]
                        address = received_transaction_split[1]
                        to_address = received_transaction_split[2]
                        amount = int(received_transaction_split[3])
                    except Exception as e:
                        app_log.info("Node: Something wrong with the transaction ("+str(e)+")")
                    #split message into values
                    received_signature_enc = data_split[1]
                    app_log.info("Node: Received signature: "+received_signature_enc)
                    received_public_key_readable = data_split[2]
                    app_log.info("Node: Received public key: "+received_public_key_readable)

                    #convert received strings
                    received_public_key = RSA.importKey(received_public_key_readable)
                    #convert received strings

                    received_signature_dec = base64.b64decode(received_signature_enc)
                    verifier = PKCS1_v1_5.new(received_public_key)
                    h = SHA.new(received_transaction)

                    if verifier.verify(h, received_signature_dec) == True:
                        app_log.info("Node: The signature is valid")
                        #transaction processing

                        #insert to mempool
                        mempool = sqlite3.connect('mempool.db')
                        m = mempool.cursor()

                        m.execute("INSERT INTO transactions VALUES ('" + str(received_timestamp) + "','" + str(address) + "','" + str(to_address) + "','" + str(amount) + "','" + str(received_signature_enc) + "','" + str(received_public_key_readable) + "')")  # Insert a row of data
                        mempool.commit()  # Save (commit) the changes
                        mempool.close()
                        app_log.info("Client: Mempool updated with a received transaction")

                        digest_mempool()
                        # insert to mempool

                        app_log.info("Node: Database closed")
                        self.request.sendall("sync_______")
                        time.sleep(0.1)

                    else:
                        app_log.info("Node: Signature invalid")
                        # todo consequences

                if data=="":
                    app_log.info("Node: Communication error")
                    raise
                time.sleep(0.1)
                #app_log.info("Server resting") #prevent cpu overload
            except Exception, e:
                app_log.info("Node: Lost connection")
                app_log.info(e)

                raise #for test purposes only ***CAUSES LEAK***
                break

#client thread
def worker(HOST,PORT):
    while True:
        try:
            connected = 0
            this_client = (HOST + ":" + str(PORT))
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            #s.settimeout(25)
            s.connect((HOST, PORT))
            app_log.info("Client: Connected to "+str(HOST)+" "+str(PORT))
            connected = 1
            if this_client not in active_pool:
                active_pool.append(this_client)
                app_log.info("Current active pool: "+str(active_pool))

            first_run=1
            while True:
                #communication starter
                if first_run==1:
                    first_run=0
                    s.sendall('helloserver')
                    time.sleep(0.1)

                #communication starter
                data = s.recv(11) #receive data, one and the only root point
                app_log.info('Client: Received data from '+ this_client)
                if data == "":
                    app_log.info("Communication error")
                    raise

                if data == "peers______":
                    subdata = s.recv(2048) #peers are larger
                    #get remote peers into tuples
                    server_peer_tuples = re.findall ("'([\d\.]+)', '([\d]+)'",subdata)
                    app_log.info(server_peer_tuples)
                    app_log.info(len(server_peer_tuples))
                    #get remote peers into tuples

                    #get local peers into tuples
                    peer_file = open("peers.txt", 'r')
                    peer_tuples = []
                    for line in peer_file:
                        extension = re.findall ("'([\d\.]+)', '([\d]+)'",line)
                        peer_tuples.extend(extension)
                    peer_file.close()
                    app_log.info(peer_tuples)
                    #get local peers into tuples

                    for x in server_peer_tuples:
                        if x not in peer_tuples:
                            app_log.info("Client: "+str(x)+" is a new peer, saving if connectible.")
                            try:
                                s_purge = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                s_purge.connect((HOST[x], PORT[x]))  # save a new peer file with only active nodes
                                s_purge.close()

                                peer_list_file = open("peers.txt", 'a')
                                peer_list_file.write(str(x)+"\n")
                                peer_list_file.close()
                            except:
                                app_log.info("Not connectible.")

                        else:
                            app_log.info("Client: "+str(x)+" is not a new peer, skipping.")


                if data == "mytxhash___":
                        data = s.recv(56) #receive client's last txhash

                        #send all our followup hashes
                        app_log.info("Client: Will seek the following block: " + str(data))
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()

                        try:
                            c.execute("SELECT * FROM transactions WHERE txhash='" + data + "'")
                            txhash_client_block = c.fetchone()[0]

                            app_log.info("Client: Node is at block "+str(txhash_client_block)) #now check if we have any newer

                            c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                            db_txhash = c.fetchone()[0] #get latest txhash
                            if db_txhash == data:
                                app_log.info("Client: Node has the latest block")
                                s.sendall("nonewblocks")
                                time.sleep(0.1)

                            else:
                                c.execute("SELECT * FROM transactions WHERE block_height='"+str(int(txhash_client_block) + 1)+"'") #select incoming transaction + 1
                                txhash_send = c.fetchone()

                                app_log.info("Client: Selected "+str(txhash_send)+" to send")

                                conn.close()
                                s.sendall("blockfound_")
                                time.sleep(0.1)

                                txhash_len = len(str(txhash_send))
                                while len(str(txhash_len)) != 10:
                                    txhash_len = "0" + str(txhash_len)
                                app_log.info("Announcing " + str(txhash_len) + " length of transaction")
                                s.sendall(str(txhash_len))
                                time.sleep(0.1)

                                s.sendall(str(txhash_send))
                                time.sleep(0.1)

                        except:
                            app_log.info("Client: Block not found")
                            s.sendall("blocknotfou")
                            time.sleep(0.1)

                if data == "sync_______":
                    #sync start

                    #send block height, receive block height
                    s.sendall("blockheight")
                    time.sleep(0.1)

                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()
                    c.execute('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1')
                    db_block_height = c.fetchone()[0]
                    conn.close()

                    app_log.info("Client: Sending block height to compare: "+str(db_block_height))
                    #append zeroes to get static length
                    while len(str(db_block_height)) != 11:
                        db_block_height = "0"+str(db_block_height)
                    s.sendall(str(db_block_height))
                    time.sleep(0.1)

                    subdata = s.recv(11) #receive node's block height
                    received_block_height = subdata
                    app_log.info("Client: Node is at block height: "+str(received_block_height))

                    #todo add to consensus pool here?
                    #todo deviation check here?
                    #todo add to active pool here?

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
                        global sync_in_progress
                        sync_in_progress = 1

                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()
                        c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                        db_txhash = c.fetchone()[0] #get latest txhash
                        conn.close()
                        app_log.info("Client: txhash to send: " +str(db_txhash))
                        s.sendall(db_txhash) #send latest txhash
                        time.sleep(0.1)

                    if update_me == 0:  # update them if update_me is 0
                        data = s.recv(56)  # receive client's last txhash

                        # send all our followup hashes
                        app_log.info("Client: Will seek the following block: " + str(data))
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()

                        try:
                            c.execute("SELECT * FROM transactions WHERE txhash='" + data + "'")
                            txhash_client_block = c.fetchone()[0]

                            app_log.info("Client: Node is at block " + str(txhash_client_block))  # now check if we have any newer

                            c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                            db_txhash = c.fetchone()[0]  # get latest txhash
                            if db_txhash == data:
                                app_log.info("Client: Node has the latest block")
                                s.sendall("nonewblocks")
                                time.sleep(0.1)

                            else:
                                c.execute("SELECT * FROM transactions WHERE block_height='" + str(int(txhash_client_block) + 1) + "'")  # select incoming transaction + 1
                                txhash_send = c.fetchone()

                                app_log.info("Node: Selected " + str(txhash_send) + " to send")

                                conn.close()
                                s.sendall("blockfound_")
                                time.sleep(0.1)

                                txhash_len = len(str(txhash_send))
                                while len(str(txhash_len)) != 10:
                                    txhash_len = "0" + str(txhash_len)
                                app_log.info("Announcing " + str(txhash_len) + " length of transaction")
                                s.sendall(str(txhash_len))
                                time.sleep(0.1)

                                s.sendall(str(txhash_send))
                                time.sleep(0.1)

                        except:
                            app_log.info("Node: Block not found")
                            s.sendall("blocknotfou")
                            time.sleep(0.1)



                if data == "blocknotfou":
                        app_log.info("Client: Node didn't find the block, deleting latest entry")
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()

                        #backup all followups to backup
                        backup = sqlite3.connect('backup.db')
                        b = backup.cursor()

                        c.execute('SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1')
                        results = c.fetchone()
                        db_block_height = results[0]
                        db_timestamp = results[1]
                        db_address = results[2]
                        db_to_address = results[3]
                        db_amount = results[4]
                        db_signature = results[5]
                        db_public_key_readable = results[6]

                        b.execute("INSERT INTO transactions VALUES ('"+str(db_timestamp)+"','"+str(db_address)+"','"+str(db_to_address)+"','"+str(db_amount)+"','"+str(db_signature)+"','"+str(db_public_key_readable) + "')") # Insert a row of data

                        backup.commit()
                        backup.close()
                        #backup all followups to backup

                        #delete followups
                        c.execute('DELETE FROM transactions WHERE block_height ="'+str(db_block_height)+'"')
                        conn.commit()
                        conn.close()
                        #delete followups
                        s.sendall("sendsync___")
                        time.sleep(0.1)

                if data == "blockfound_":
                    app_log.info("Client: Node has the block") #node should start sending txs in this step

                    data = s.recv(10)
                    app_log.info("Transaction length to receive: " + data)
                    txhash_len = int(data)
                    data = s.recv(txhash_len)

                    app_log.info("Client: "+data)
                    #verify
                    sync_list = ast.literal_eval(data) #this is great, need to add it to client -> node sync
                    received_block_height = sync_list[0]
                    received_timestamp = sync_list[1]
                    received_address = sync_list[2]
                    received_to_address = sync_list[3]
                    received_amount = sync_list [4]
                    received_signature_enc = sync_list[5]
                    received_public_key_readable = sync_list[6]
                    received_public_key = RSA.importKey(sync_list[6])
                    received_transaction = str(received_timestamp) +":"+ str(received_address) +":"+ str(received_to_address) +":"+ str(received_amount) #todo: why not have bare list instead of converting?

                    #txhash validation start

                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()
                    c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
                    txhash_db = c.fetchone()[0]

                    #backup all followups to backup
                    backup = sqlite3.connect('backup.db')
                    b = backup.cursor()

                    for row in c.execute('SELECT * FROM transactions WHERE block_height > "'+str(received_block_height)+'"'):
                        db_timestamp = row[1]
                        db_address = row[2]
                        db_to_address = row[3]
                        db_amount = row [4]
                        db_signature = row[5]
                        db_public_key_readable = row[6]

                        b.execute("INSERT INTO transactions VALUES ('"+str(db_timestamp)+"','"+str(db_address)+"','"+str(db_to_address)+"','"+str(db_amount)+"','"+str(db_signature)+"','"+str(db_public_key_readable) + "')") # Insert a row of data

                    backup.commit()
                    backup.close()
                    #backup all followups to backup

                    #delete all local followups
                    c.execute('DELETE FROM transactions WHERE block_height > "'+str(received_block_height)+'"')
                    conn.close()
                    #delete all local followups

                    app_log.info("Client: Last db txhash: "+str(txhash_db))
                    app_log.info("Client: Received transaction: "+str(received_transaction))

                    received_signature_dec = base64.b64decode(received_signature_enc)
                    verifier = PKCS1_v1_5.new(received_public_key)
                    h = SHA.new(received_transaction)

                    if verifier.verify(h, received_signature_dec) == True:
                        app_log.info("Node: The signature is valid")
                        # transaction processing

                        # insert to mempool
                        mempool = sqlite3.connect('mempool.db')
                        m = mempool.cursor()
                        m.execute("INSERT INTO transactions VALUES ('"+str(received_timestamp)+"','"+str(received_address)+"','"+str(received_to_address)+"','"+str(received_amount)+"','"+str(received_signature_enc)+"','"+str(received_public_key_readable) + "')") # Insert a row of data
                        app_log.info("Client: Mempool updated with a received transaction")
                        mempool.commit() # Save (commit) the changes
                        mempool.close()

                        digest_mempool()
                        # insert to mempool

                        s.sendall("sendsync___")
                        time.sleep(0.1)

                    else:
                        app_log.info("Client: Received invalid signature")
                        #rollback end

                    #txhash validation end

                if data == "nonewblocks":
                    app_log.info("Restoring local transactions from backup")
                    app_log.info("Client: We seem to be at the latest block. Paused before recheck.")

                    if sync_in_progress == 1:
                        restore_backup() #restores backup and digests mempool
                    else:
                        app_log.info("Backup txs are waiting for sync to finish")


                    time.sleep(10)
                    s.sendall("sendsync___")
                    time.sleep(0.1)
        except Exception as e:

            if connected == 1:
                app_log.info("Will remove " + str(this_client) + " from active pool " + str(active_pool))
                active_pool.remove(this_client)

                # remove from consensus
                if this_client in consensus_ip_list:
                    app_log.info("Will remove " + str(this_client) + " from consensus pool " + str(consensus_ip_list))
                    consensus_index = consensus_ip_list.index(this_client)
                    del consensus_ip_list[consensus_index]  # remove ip
                    del consensus_opinion_list[consensus_index]  # remove ip's opinion
                    # remove from consensus
                else:
                    app_log.info("Client " + str(this_client) + " not present in the consensus pool")


            app_log.info("Connection to "+this_client+" terminated due to "+ str(e))
            app_log.info("---thread "+str(threading.currentThread())+" ended---")
            raise #test only
            return
            
    return

class ThreadedTCPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    pass



if __name__ == "__main__":
    try:
        # Port 0 means to select an arbitrary unused port
        HOST, PORT = "0.0.0.0", port

        server = ThreadedTCPServer((HOST, PORT), ThreadedTCPRequestHandler)
        ip, port = server.server_address

        # Start a thread with the server -- that thread will then start one
        # more thread for each request
        
        server_thread = threading.Thread(target=server.serve_forever)

        # Exit the server thread when the main thread terminates    
        
        server_thread.daemon = True
        server_thread.start()
        app_log.info("Server loop running in thread: "+ server_thread.name)

        #start connection manager
        t_manager = threading.Thread(target=manager())
        app_log.info("Starting connection manager")
        t_manager.start()
        #start connection manager

        #server.serve_forever() #added
        server.shutdown()
        server.server_close()

    except Exception, e:
        app_log.info("Node already running?")
        app_log.info(e)
        raise #only test
sys.exit()