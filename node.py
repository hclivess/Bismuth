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

from Crypto import Random
from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5

gc.enable()

logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.DEBUG) #,filename='node.log'

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
            #logging.info(peer_tuples)

            threads_count = threading.active_count()
            threads_limit = 15

            for tuple in peer_tuples:
                HOST = tuple[0]
                #logging.info(HOST)
                PORT = int(tuple[1])
                #logging.info(PORT)

                logging.info(HOST+":"+str(PORT))
                if threads_count <= threads_limit and str(HOST+":"+str(PORT)) not in tried:  # minus server thread, client thread, connectivity manager thread
                    tried.append(HOST+":"+str(PORT))
                    t = threading.Thread(target=worker, args=(HOST,PORT))#threaded connectivity to nodes here
                    logging.info("---Starting a client thread "+str(threading.currentThread())+"---")
                    t.start()

            #client thread handling
        logging.info("Connection manager: Threads at " + str(threads_count) + "/" + str(threads_limit))
        logging.info("Tried: " + str(tried))
        #logging.info(threading.enumerate() all threads)
        time.sleep(10)
    return

def digest_mempool():
    #digest mempool start
    while True:
        logging.info("Node: Digesting mempool")
        mempool = sqlite3.connect('mempool.db')
        m = mempool.cursor()

        m.execute("SELECT signature FROM transactions ORDER BY block_height DESC LIMIT 1;")

        try:
            signature_mempool = m.fetchone()[0]
        except:
            logging.info("Mempool empty")
            break   

        conn = sqlite3.connect('ledger.db')
        c = conn.cursor()
        c.execute("SELECT * FROM transactions WHERE signature ='"+signature_mempool+"';")
        try:
            txhash_match = c.fetchone()[0]
           
            logging.info("Mempool: tx sig found in the local ledger, deleting tx")
            m.execute("DELETE FROM transactions WHERE signature ='"+signature_mempool+"';")
            mempool.commit()

        except:
            logging.info("Mempool: tx sig not found in the local ledger, proceeding to insert")

            #calculate block height from the ledger
            for row in c.execute('SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1;'):
                db_block_height = row[0]
                db_txhash = row[7]

            for row in m.execute("SELECT * FROM transactions WHERE signature = '"+signature_mempool+"';"):
                db_timestamp = row[1]
                db_address = row[2]
                db_to_address = row[3]
                db_amount = row[4]
                db_signature = row[5]
                db_public_key_readable = row[6]
                #db_public_key = RSA.importKey(row[6])
                db_transaction = str(db_timestamp) +":"+ str(db_address) +":"+ str(db_to_address) +":"+ str(db_amount)
                txhash = hashlib.sha224(str(db_transaction) + str(db_signature) +str(db_txhash)).hexdigest() #calculate txhash from the ledger

                #verify balance
                logging.info("Mempool: Verifying balance")
                logging.info("Mempool: Received address: " + str(db_address))
                c.execute("SELECT sum(amount) FROM transactions WHERE to_address = '" + db_address + "'")
                credit = c.fetchone()[0]
                c.execute("SELECT sum(amount) FROM transactions WHERE address = '" + db_address + "'")
                debit = c.fetchone()[0]
                if debit == None:
                    debit = 0
                if credit == None:
                    credit = 0
                logging.info("Mempool: Total credit: " + str(credit))
                logging.info("Mempool: Total debit: " + str(debit))
                balance = int(credit) - int(debit)
                logging.info("Node: Transction address balance: " + str(balance))
                conn.close()

                if int(balance) - int(db_amount) < 0:
                    logging.info("Mempool: Their balance is too low for this transaction, possible double spend attack")
                elif int(db_amount) < 0:
                    logging.info("Mempool: Cannot use negative amounts")
                #verify balance
                else:
                    c.execute("INSERT INTO transactions VALUES ('"+str(db_block_height+1)+"','"+str(db_timestamp)+"','"+str(db_address)+"','"+str(db_to_address)+"','"+str(db_amount)+"','"+str(db_signature)+"','"+str(db_public_key_readable)+"','"+str(txhash)+"')") # Insert a row of data
                    conn.commit()
                    conn.close()

                m.execute("DELETE FROM transactions WHERE txhash = '"+db_txhash+"';") #delete tx from mempool now that it is in the ledger or if it was a double spend
                mempool.commit()
                mempool.close()
    return
        
def db_maintenance():
    #db maintenance
    conn=sqlite3.connect("ledger.db")
    conn.execute("VACUUM")
    conn.close()
    conn=sqlite3.connect("mempool.db")
    conn.execute("VACUUM")
    conn.close()
    logging.info("Core: Database maintenance finished")
    
#key maintenance
if os.path.isfile("privkey.der") is True:
            logging.info("Client: privkey.der found")
else:   
    #generate key pair and an address
    random_generator = Random.new().read
    key = RSA.generate(1024, random_generator)
    public_key = key.publickey()

    private_key_readable = str(key.exportKey())
    public_key_readable = str(key.publickey().exportKey())
    address = hashlib.sha224(public_key_readable).hexdigest() #hashed public key
    #generate key pair and an address

    logging.info("Client: Your address: "+ str(address))
    logging.info("Client: Your private key: "+ str(private_key_readable))
    logging.info("Client: Your public key: "+ str(public_key_readable))

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

logging.info("Client: Local address: "+ str(address))



db_maintenance()
#connectivity to self node

#verify blockchain
con = None
conn = sqlite3.connect('ledger.db')
c = conn.cursor()
#c.execute("CREATE TABLE IF NOT EXISTS transactions (block_height, address, to_address, amount, signature, public_key)")
c.execute("SELECT Count(*) FROM transactions")
db_rows = c.fetchone()[0]
logging.info("Core: Total steps: "+str(db_rows))

#create empty mempool
mempool = sqlite3.connect('mempool.db')
m = mempool.cursor()
m.execute("CREATE TABLE IF NOT EXISTS transactions (block_height, address, to_address, amount, signature, public_key)")
mempool.commit()
mempool.close()
logging.info("Core: Created mempool file")
#create empty mempool

#verify genesis
c.execute("SELECT to_address FROM transactions ORDER BY block_height ASC LIMIT 1")
genesis = c.fetchone()[0]
logging.info("Core: Genesis: "+genesis)
if str(genesis) != "824437b7fb468bd5e584d80a091c9bac4085b3e48d7aa9182319473a": #change this line to your genesis address if you want to clone
    logging.info("Core: Invalid genesis address")
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

        #logging.info(db_transaction)

        invalid = 0

        db_signature_dec = base64.b64decode(db_signature_enc)
        verifier = PKCS1_v1_5.new(db_public_key)
        h = SHA.new(db_transaction)
        if verifier.verify(h, db_signature_dec) == True:
            pass
        else:
            invalid = invalid + 1
            if db_block_height == str(1):
                logging.info("Core: Your genesis signature is invalid, someone meddled with the database")
                sys.exit(1)

    if invalid > 0:
        logging.info("Core: "+str(invalid)+" of the transactions in the local ledger are invalid")

    if invalid == 0:
        logging.info("Core: All transacitons in the local ledger are valid")
        
except sqlite3.Error, e:                        
    logging.info("Core: Error %s:" % e.args[0])
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
                
                logging.info("Node: Received: "+data+ " from " +str(self.request.getpeername()[0])) #will add custom ports later

                if data == 'helloserver':
                    with open ("peers.txt", "r") as peer_list:
                        peers=peer_list.read()
                        logging.info("Node: "+peers)
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
                        logging.info("Testing connectivity to: "+str(peer_ip))
                        peer_test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        peer_test.connect((str(peer_ip), 2829)) #double parentheses mean tuple
                        logging.info("Node: Distant peer connectible")
                        if peer_tuple not in str(peer_tuples): #stringing tuple is a nasty way
                            peer_list_file = open("peers.txt", 'a')
                            peer_list_file.write((peer_tuple)+"\n")
                            logging.info("Node: Distant peer saved to peer list")
                            peer_list_file.close()
                        else:
                            logging.info("Core: Distant peer already in peer list")
                    except:
                        logging.info("Node: Distant peer not connectible")
                        #raise #test only

                    #save peer if connectible


                        
                    logging.info("Node: Sending sync request")
                    self.request.sendall("sync_______")
                    time.sleep(0.1)

                if data == "sendsync___":
                    self.request.sendall("sync_______")
                    time.sleep(0.1)                  

                if data == "blockfound_":                  
                    logging.info("Node: Client has the block") #client should start sending txs in this step
                    #todo critical: make sure that received block height is correct
                    data = self.request.recv(2048)
                    #verify
                    sync_list = ast.literal_eval(data) #this is great, need to add it to client -> node sync
                    received_block_height = sync_list[0]
                    received_timestamp = sync_list[1]
                    received_address = sync_list[2]
                    received_to_address = sync_list[3]
                    received_amount = sync_list [4]
                    received_signature_enc = sync_list[5]
                    received_public_key_readable = sync_list[6]
                    #received_public_key = RSA.importKey(sync_list[6])
                    received_txhash = sync_list[7]
                    received_transaction = str(received_timestamp) +":"+ str(received_address) +":"+ str(received_to_address) +":"+ str(received_amount) #todo: why not have bare list instead of converting?

                    #txhash validation start

                    #open dbs for mempool backup and followup deletion
                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()
                    c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
                    txhash_db = c.fetchone()[0]

                    #backup all followups to mempool
                    mempool = sqlite3.connect('mempool.db')
                    m = mempool.cursor()

                    for row in c.execute('SELECT * FROM transactions WHERE block_height > "'+str(received_block_height)+'"'):
                        db_block_height = row[0]
                        db_timestamp = row[1]
                        db_address = row[2]
                        db_to_address = row[3]
                        db_amount = row [4]
                        db_signature = row[5]
                        db_public_key_readable = row[6]
                        db_public_key = RSA.importKey(row[6])
                        db_txhash = row[7]
                        db_transaction = str(db_timestamp) +":"+ str(db_address) +":"+ str(db_to_address) +":"+ str(db_amount) 

                        m.execute("INSERT INTO transactions VALUES ('"+str(db_block_height)+"','"+str(db_timestamp)+"','"+str(db_address)+"','"+str(db_to_address)+"','"+str(db_amount)+"','"+str(db_signature)+"','"+str(db_public_key_readable)+"','"+str(db_txhash)+"')") # Insert a row of data

                    mempool.commit()
                    mempool.close()
                    #backup all followups to mempool
                    
                    #delete all local followups
                    c.execute('DELETE FROM transactions WHERE block_height > "'+str(received_block_height)+'"')
                    conn.close()
                    #delete all local followups
                    
                    logging.info("Node: Last db txhash: "+str(txhash_db))
                    logging.info("Node: Received txhash: "+str(received_txhash))
                    logging.info("Node: Received transaction: "+str(received_transaction))

                    if received_txhash == hashlib.sha224(str(received_transaction) + str(received_signature_enc) +str(txhash_db)).hexdigest(): #new hash = new tx + new sig + old txhash
                        logging.info("Node: txhash valid")

                        #update local db with received tx
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()

                        #duplicity verification
                        logging.info("verifying duplicity")
                        c.execute("SELECT signature FROM transactions WHERE signature = '"+received_signature_enc+"'")
                        try:
                            c.fetchone()[0]
                            logging.info("Duplicate transaciton")
                        except:
                            logging.info("Node: Not a duplicate")
                            #duplicity verification
                                
                            logging.info("Node: Verifying balance")
                            logging.info("Node: Received address: " +str(received_address))
                            c.execute("SELECT sum(amount) FROM transactions WHERE to_address = '"+received_address+"'")
                            credit = c.fetchone()[0]
                            c.execute("SELECT sum(amount) FROM transactions WHERE address = '"+received_address+"'")
                            debit = c.fetchone()[0]
                            if debit == None:
                                debit = 0
                            if credit == None:
                                credit = 0                                
                            logging.info("Node: Total credit: "+str(credit))
                            logging.info("Node: Total debit: "+str(debit))
                            balance = int(credit) - int(debit)
                            logging.info("Node: Transction address balance: "+str(balance))
                            conn.close()
                                    
                            if  int(balance) - int(received_amount) < 0:
                                logging.info("Node: Their balance is too low for this transaction")
                            elif int(received_amount) < 0:
                                logging.info("Node: Cannot use negative amounts")
                            else:                              
                                #save step to db
                                conn = sqlite3.connect('ledger.db') 
                                c = conn.cursor()
                                c.execute("INSERT INTO transactions VALUES ('"+str(received_block_height)+"','"+str(received_timestamp)+"','"+str(received_address)+"','"+str(received_to_address)+"','"+str(received_amount)+"','"+str(received_signature_enc)+"','"+str(received_public_key_readable)+"','"+str(received_txhash)+"')") # Insert a row of data
                                logging.info("Node: Ledger updated with a received transaction")
                                conn.commit() # Save (commit) the changes
                                conn.close()
                                #save step to db
                                logging.info("Node: Ledger synchronization finished")
                                digest_mempool()

                                logging.info("Node: Sending sync request")
                                self.request.sendall("sync_______")
                                time.sleep(0.1)
                                #update local db with received tx


                        
                    else:
                        logging.info("Node: txhash invalid")
                        #rollback start
                        logging.info("Node: Received invalid txhash")
                        #rollback end

                if data == "blockheight":
                    subdata = self.request.recv(11) #receive client's last block height
                    received_block_height = subdata
                    logging.info("Node: Received block height: "+(received_block_height))
                    #send own block height
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
                        logging.info("Node: Client has higher block, receiving")
                        update_me = 1
                        #todo
                        
                    if received_block_height < db_block_height:
                        logging.info("Node: We have a higher block, hash will be verified")
                        update_me = 0

                    if received_block_height == db_block_height:
                        logging.info("Node: We have the same block height, hash will be verified")
                        update_me = 0

                    if update_me == 1:
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()                
                        c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                        db_txhash = c.fetchone()[0] #get latest txhash
                        conn.close()
                        logging.info("Node: txhash to send: " +str(db_txhash))
                        self.request.sendall("mytxhash__")
                        time.sleep(0.1)
                        self.request.sendall(db_txhash) #send latest txhash
                        time.sleep(0.1)

                    if update_me == 0: #update them if update_me is 0
                        data = self.request.recv(56) #receive client's last txhash

                        # consensus pool
                        consensus_ip = self.request.getpeername()[0]
                        consensus_opinion = data
                        tried_ips = []
                        for x in tried:
                            if x.split(":")[0] not in tried_ips:
                                tried_ips.append(x.split(":")[0])

                        #logging.info(str(tried_ips))
                        #logging.info(consensus_ip)


                        if consensus_ip in tried_ips and consensus_ip in consensus_ip_list:
                            consensus_index = consensus_ip_list.index(consensus_ip)  # get where in this list it is
                            if consensus_opinion_list[consensus_index] == consensus_opinion:
                                logging.info("IP's opinion hasn't changed")

                            else:
                                del consensus_ip_list[consensus_index]  # remove ip
                                del consensus_opinion_list[consensus_index]  # remove ip's opinion
                                logging.info("Updating " + str(consensus_ip) + " in consensus")
                                consensus_ip_list.append(consensus_ip)
                                consensus_opinion_list.append(consensus_opinion)

                        if consensus_ip not in consensus_ip_list:
                            logging.info("Adding " + str(consensus_ip) + " to consensus peer list")
                            consensus_ip_list.append(consensus_ip)
                            logging.info("Assigning " + str(consensus_opinion) + " to peer's opinion list")
                            consensus_opinion_list.append(consensus_opinion)



                        # consensus pool

                        #send all our followup hashes
                        logging.info("Node: Will seek the following block: " + str(data))
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()

                        c.execute("SELECT * FROM transactions WHERE txhash='"+data+"'")
                        try:
                            txhash_client_block = c.fetchone()[0]

                            logging.info("Node: Client is at block "+str(txhash_client_block)) #now check if we have any newer

                            c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                            db_txhash = c.fetchone()[0] #get latest txhash
                            if db_txhash == data:
                                logging.info("Node: Client has the latest block")
                                self.request.sendall("nonewblocks")
                                time.sleep(0.1)
             
                            else:
                                c.execute("SELECT * FROM transactions WHERE block_height='"+str(int(txhash_client_block) + 1)+"'") #select incoming transaction + 1
                                txhash_send = c.fetchone()

                                logging.info("Node: Selected "+str(txhash_send)+" to send")
                                
                                conn.close()
                                self.request.sendall("blockfound_")
                                time.sleep(0.1)
                                self.request.sendall(str(txhash_send))
                                time.sleep(0.1)
                            
                        except:
                            logging.info("Node: Block not found")
                            self.request.sendall("blocknotfou")
                            time.sleep(0.1)
                            #todo send previous

                if data == "blocknotfou":
                    logging.info("Client: Node didn't find the block, deleting latest entry")
                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()
                    c.execute('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1')
                    db_block_height = c.fetchone()[0]


                    #backup all followups to mempool
                    mempool = sqlite3.connect('mempool.db')
                    m = mempool.cursor()

                    c.execute('SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1')
                    results = c.fetchone()
                    db_block_height = results[0]
                    db_timestamp = results[1]
                    db_address = results[2]
                    db_to_address = results[3]
                    db_amount = results[4]
                    db_signature = results[5]
                    db_public_key_readable = results[6]
                    db_public_key = RSA.importKey(results[6])
                    db_txhash = results[7]
                    db_transaction = str(db_timestamp) +":"+ str(db_address) +":"+ str(db_to_address) +":"+ str(db_amount)

                    txhash = hashlib.sha224(str(db_transaction) + str(db_signature) +str(db_txhash)).hexdigest() #calculate new txhash from ledger latest tx and the new tx

                    m.execute("INSERT INTO transactions VALUES ('"+str(int(db_block_height)+1)+"','"+str(db_timestamp)+"','"+str(db_address)+"','"+str(db_to_address)+"','"+str(db_amount)+"','"+str(db_signature)+"','"+str(db_public_key_readable)+"','"+str(txhash)+"')") # Insert a row of data

                    mempool.commit()
                    mempool.close()
                    #backup all followups to mempool

                    #delete followups
                    c.execute('DELETE FROM transactions WHERE block_height ="'+str(db_block_height)+'"')
                    conn.commit()
                    conn.close()
                    #delete followups
                    logging.info("Client: Deletion complete, sending sync request")
                    self.request.sendall("sync_______")
                    time.sleep(0.1)

                            
                #latest local block          
                if data == "transaction":
                    data = self.request.recv(2048)
                    data_split = data.split(";")
                    received_transaction = data_split[0]
                    logging.info("Node: Received transaction: "+received_transaction)
                    #split message into values
                    try:
                        received_transaction_split = received_transaction.split(":")#todo receive list
                        received_timestamp = received_transaction_split[0]
                        address = received_transaction_split[1]
                        to_address = received_transaction_split[2]
                        amount = int(received_transaction_split[3])
                    except Exception as e:
                        logging.info("Node: Something wrong with the transaction ("+str(e)+")")
                    #split message into values
                    received_signature_enc = data_split[1]
                    logging.info("Node: Received signature: "+received_signature_enc)
                    received_public_key_readable = data_split[2]
                    logging.info("Node: Received public key: "+received_public_key_readable)
                    received_txhash = data_split[3]
                    logging.info("Node: Received txhash: "+received_txhash)

                    #convert received strings
                    received_public_key = RSA.importKey(received_public_key_readable)
                    #convert received strings

                    db_signature_dec = base64.b64decode(received_signature_enc)
                    verifier = PKCS1_v1_5.new(received_public_key)
                    h = SHA.new(received_transaction)
                    
                    if verifier.verify(h, db_signature_dec) == True:
                        logging.info("Node: The signature is valid")
                        #transaction processing

                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()

                        #duplicity verification
                        logging.info("verifying duplicity")
                        c.execute("SELECT signature FROM transactions WHERE signature = '"+received_signature_enc+"'")
                        try:
                            c.fetchone()[0]
                            logging.info("Duplicate transaciton")
                        except:
                            logging.info("Node: Not a duplicate")
                            #duplicity verification
                     
                            #verify balance and blockchain                           
                            logging.info("Node: Verifying balance")
                            logging.info("Node: Address:" +address)
                            c.execute("SELECT sum(amount) FROM transactions WHERE to_address = '"+address+"'")
                            credit = c.fetchone()[0]
                            c.execute("SELECT sum(amount) FROM transactions WHERE address = '"+address+"'")
                            debit = c.fetchone()[0]
                            if debit == None:
                                debit = 0
                            if credit == None:
                                credit = 0                                
                            logging.info("Node: Total credit: "+str(credit))
                            logging.info("Node: Total debit: "+str(debit))
                            balance = int(credit) - int(debit)
                            logging.info("Node: Your balance: "+str(balance))

                            if  int(balance) - int(amount) < 0:
                                logging.info("Node: Your balance is too low for this transaction")
                            elif int(amount) < 0:
                                logging.info("Node: Cannot use negative amounts")
                            else:
                                logging.info("Node: Processing transaction")

                                c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                                txhash = c.fetchone()[0]
                                c.execute('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1')
                                block_height = c.fetchone()[0]
                                logging.info("Node: Current latest txhash: "+str(txhash))
                                logging.info("Node: Current top block: " +str(block_height))
                                block_height_new = block_height + 1
                                
                                

                                if received_txhash == hashlib.sha224(str(received_transaction) + str(received_signature_enc) +str(txhash)).hexdigest(): #new hash = new tx + new sig + old txhash
                                    logging.info("Node: txhash valid")
                                    txhash_valid = 1
                                    
                                    c.execute("INSERT INTO transactions VALUES ('"+str(block_height_new)+"','"+str(received_timestamp)+"','"+str(address)+"','"+str(to_address)+"','"+str(amount)+"','"+str(received_signature_enc)+"','"+str(received_public_key_readable)+"','"+str(received_txhash)+"')") # Insert a row of data                    
                                    #execute transaction                                
                                    conn.commit() # Save (commit) the changes
                                    #todo: broadcast
                                    logging.info("Node: Saved")

                                    conn.close()
                                    logging.info("Node: Database closed")
                                    self.request.sendall("sync_______")
                                    time.sleep(0.1)
                                            
                                    #transaction processing                        
                                
                                else:
                                    logging.info("Node: txhash invalid")
                                    conn.close()
                                                                
                                #verify balance and blockchain                            
                                    #execute transaction
                            

                    else:
                        logging.info("Node: Signature invalid")

                if data=="":
                    logging.info("Node: Communication error")
                    return
                time.sleep(0.1)
                #logging.info("Server resting") #prevent cpu overload
            except Exception, e:
                logging.info("Node: Lost connection")
                logging.info(e)
                raise #for test purposes only ***CAUSES LEAK***
                break                        

#client thread
def worker(HOST,PORT):
    while True:
        try:        
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            #s.settimeout(25)
            s.connect((HOST, PORT))
            logging.info("Client: Connected to "+str(HOST)+" "+str(PORT))

            first_run=1
            while True:
                #communication starter   
                if first_run==1:
                    first_run=0
                    s.sendall('helloserver')
                    time.sleep(0.1)
                    peer = s.getpeername()
                
                #communication starter



                data = s.recv(11) #receive data, one and the only root point
                logging.info('Client: Received data from '+ str(peer) +": "+ str(data))
                if data == "":
                    logging.info("Communication error")
                    raise
                    
                if data == "peers______":
                    subdata = s.recv(2048) #peers are larger 
                    #get remote peers into tuples
                    server_peer_tuples = re.findall ("'([\d\.]+)', '([\d]+)'",subdata)
                    logging.info(server_peer_tuples)
                    logging.info(len(server_peer_tuples))
                    #get remote peers into tuples

                    #get local peers into tuples
                    peer_file = open("peers.txt", 'r')
                    peer_tuples = []
                    for line in peer_file:
                        extension = re.findall ("'([\d\.]+)', '([\d]+)'",line)
                        peer_tuples.extend(extension)
                    peer_file.close()
                    logging.info(peer_tuples)
                    #get local peers into tuples

                    for x in server_peer_tuples:
                        if x not in peer_tuples:
                            logging.info("Client: "+str(x)+" is a new peer, saving if connectible.")
                            try:
                                s_purge = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                                s_purge.connect((HOST[x], PORT[x]))  # save a new peer file with only active nodes
                                s_purge.close()

                                peer_list_file = open("peers.txt", 'a')
                                peer_list_file.write(str(x)+"\n")
                                peer_list_file.close()
                            except:
                                logging.info("Not connectible.")
                            
                        else:
                            logging.info("Client: "+str(x)+" is not a new peer, skipping.")


                if data == "mytxhash__":
                        data = s.recv(56) #receive client's last txhash

                        #send all our followup hashes
                        logging.info("Client: Will seek the following block: " + str(data))
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()

                        c.execute("SELECT * FROM transactions WHERE txhash='"+data+"'")
                        try:
                            txhash_client_block = c.fetchone()[0]

                            logging.info("Client: Node is at block "+str(txhash_client_block)) #now check if we have any newer

                            c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                            db_txhash = c.fetchone()[0] #get latest txhash
                            if db_txhash == data:
                                logging.info("Client: Node has the latest block")
                                s.sendall("nonewblocks")
                                time.sleep(0.1)
             
                            else:
                                c.execute("SELECT * FROM transactions WHERE block_height='"+str(int(txhash_client_block) + 1)+"'") #select incoming transaction + 1
                                txhash_send = c.fetchone()

                                logging.info("Client: Selected "+str(txhash_send)+" to send")
                                
                                conn.close()
                                s.sendall("blockfound_")
                                time.sleep(0.1)
                                s.sendall(str(txhash_send))
                                time.sleep(0.1)
                            
                        except:
                            logging.info("Client: Block not found")
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
                    
                    logging.info("Client: Sending block height to compare: "+str(db_block_height))
                    #append zeroes to get static length
                    while len(str(db_block_height)) != 11:
                        db_block_height = "0"+str(db_block_height)
                    s.sendall(str(db_block_height))
                    time.sleep(0.1)
                    
                    subdata = s.recv(11) #receive node's block height
                    received_block_height = subdata
                    logging.info("Client: Node is at block height: "+str(received_block_height))

                    if received_block_height < db_block_height:
                        logging.info("Client: We have a higher, sending")
                        update_me = 0
                        #todo
                    
                    if received_block_height > db_block_height:
                        logging.info("Client: Node has higher block, receiving")
                        update_me = 1
                        #todo

                    if received_block_height == db_block_height:
                        logging.info("Client: We have the same block height, hash will be verified")
                        update_me = 1
                        #todo                

                    if update_me == 1:                
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()                
                        c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                        db_txhash = c.fetchone()[0] #get latest txhash
                        conn.close()
                        logging.info("Client: txhash to send: " +str(db_txhash))
                        
                        s.sendall(db_txhash) #send latest txhash
                        time.sleep(0.1)

                if data == "blocknotfou":
                        logging.info("Client: Node didn't find the block, deleting latest entry")
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()
                        c.execute('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1')
                        db_block_height = c.fetchone()[0]

                        #backup all followups to mempool
                        mempool = sqlite3.connect('mempool.db')
                        m = mempool.cursor()

                        c.execute('SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1')
                        results = c.fetchone()
                        db_block_height = results[0]
                        db_timestamp = results[1]
                        db_address = results[2]
                        db_to_address = results[3]
                        db_amount = results[4]
                        db_signature = results[5]
                        db_public_key_readable = results[6]
                        db_public_key = RSA.importKey(results[6])
                        db_txhash = results[7]
                        db_transaction = str(db_timestamp) +":"+ str(db_address) +":"+ str(db_to_address) +":"+ str(db_amount)

                        m.execute("INSERT INTO transactions VALUES ('"+str(db_block_height)+"','"+str(db_timestamp)+"','"+str(db_address)+"','"+str(db_to_address)+"','"+str(db_amount)+"','"+str(db_signature)+"','"+str(db_public_key_readable)+"','"+str(db_txhash)+"')") # Insert a row of data

                        mempool.commit()
                        mempool.close()
                        #backup all followups to mempool

                        #delete followups
                        c.execute('DELETE FROM transactions WHERE block_height ="'+str(db_block_height)+'"')
                        conn.commit()
                        conn.close()
                        #delete followups
                        s.sendall("sendsync___") #experimental
                        time.sleep(0.1)

                if data == "blockfound_":          
                    logging.info("Client: Node has the block") #node should start sending txs in this step
                    #todo critical: make sure that received block height is correct
                    data = s.recv(2048)
                    logging.info("Client: "+data)
                    #verify
                    sync_list = ast.literal_eval(data) #this is great, need to add it to client -> node sync
                    received_block_height = sync_list[0]
                    received_timestamp = sync_list[1]
                    received_address = sync_list[2]
                    received_to_address = sync_list[3]
                    received_amount = sync_list [4]
                    received_signature = sync_list[5]
                    received_public_key_readable = sync_list[6]
                    received_public_key = RSA.importKey(sync_list[6])
                    received_txhash = sync_list[7]
                    received_transaction = str(received_timestamp) +":"+ str(received_address) +":"+ str(received_to_address) +":"+ str(received_amount) #todo: why not have bare list instead of converting?

                    #txhash validation start

                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()
                    c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
                    txhash_db = c.fetchone()[0]
                    
                    #backup all followups to mempool
                    mempool = sqlite3.connect('mempool.db')
                    m = mempool.cursor()

                    for row in c.execute('SELECT * FROM transactions WHERE block_height > "'+str(received_block_height)+'"'):
                        db_block_height = row[0]
                        db_timestamp = row[1]
                        db_address = row[2]
                        db_to_address = row[3]
                        db_amount = row [4]
                        db_signature = row[5]
                        db_public_key_readable = row[6]
                        db_public_key = RSA.importKey(row[6])
                        db_txhash = row[7]
                        db_transaction = str(db_timestamp) +":"+ str(db_address) +":"+ str(db_to_address) +":"+ str(db_amount) 

                        m.execute("INSERT INTO transactions VALUES ('"+str(db_block_height)+"','"+str(db_timestamp)+"','"+str(db_address)+"','"+str(db_to_address)+"','"+str(db_amount)+"','"+str(db_signature)+"','"+str(db_public_key_readable)+"','"+str(db_txhash)+"')") # Insert a row of data

                    mempool.commit()
                    mempool.close()
                    #backup all followups to mempool       
                    
                    #delete all local followups
                    c.execute('DELETE FROM transactions WHERE block_height > "'+str(received_block_height)+'"')
                    conn.close()
                    #delete all local followups
                    
                    logging.info("Client: Last db txhash: "+str(txhash_db))
                    logging.info("Client: Received txhash: "+str(received_txhash))
                    logging.info("Client: Received transaction: "+str(received_transaction))

                    txhash_valid = 0
                    if received_txhash == hashlib.sha224(str(received_transaction) + str(received_signature) +str(txhash_db)).hexdigest(): #new hash = new tx + new sig + old txhash
                        logging.info("Client: txhash valid")
                        txhash_valid = 1

                        #update local db with received tx
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()

                        #duplicity verification
                        logging.info("verifying duplicity")
                        c.execute("SELECT signature FROM transactions WHERE signature = '"+received_signature+"'")
                        try:
                            c.fetchone()[0]
                            logging.info("Duplicate transaction")
                        except:
                            logging.info("Client: Not a duplicate")
                            #duplicity verification
                        
                        logging.info("Client: Verifying balance")
                        logging.info("Client: Received address: " +str(received_address))
                        c.execute("SELECT sum(amount) FROM transactions WHERE to_address = '"+received_address+"'")
                        credit = c.fetchone()[0]
                        c.execute("SELECT sum(amount) FROM transactions WHERE address = '"+received_address+"'")
                        debit = c.fetchone()[0]
                        if debit == None:
                            debit = 0
                        if credit == None:
                            credit = 0                                
                        logging.info("Client: Total credit: "+str(credit))
                        logging.info("Client: Total debit: "+str(debit))
                        balance = int(credit) - int(debit)
                        logging.info("Client: Transction address balance: "+str(balance))
                        conn.close()
                                
                        if  int(balance) - int(received_amount) < 0:
                            logging.info("Client: Their balance is too low for this transaction")
                        elif int(received_amount) < 0:
                            logging.info("Client: Cannot use negative amounts")
                        else:                              
                            #save step to db
                            conn = sqlite3.connect('ledger.db') 
                            c = conn.cursor()
                            c.execute("INSERT INTO transactions VALUES ('"+str(received_block_height)+"','"+str(received_timestamp)+"','"+str(received_address)+"','"+str(received_to_address)+"','"+str(received_amount)+"','"+str(received_signature)+"','"+str(received_public_key_readable)+"','"+str(received_txhash)+"')") # Insert a row of data
                            logging.info("Client: Ledger updated with a received transaction")
                            conn.commit() # Save (commit) the changes
                            conn.close()
                            #save step to db
                            logging.info("Client: Ledger synchronization finished")
                            digest_mempool()

                            s.sendall("sendsync___")
                            time.sleep(0.1)

                    else:
                        logging.info("Client: Received invalid txhash")
                        #rollback end
                                
                    #txhash validation end

                if data == "nonewblocks":
                    logging.info("Client: We seem to be at the latest block. Paused before recheck.")
                    time.sleep(10)
                    s.sendall("sendsync___")
                    time.sleep(0.1)
        except Exception as e:
            logging.info("Thread terminated due to "+ str(e))
            this_client = (HOST+":"+str(PORT))
            logging.info("Will remove "+str(this_client) +" from "+str(tried))
            tried.remove(str(this_client))

            # remove from consensus
            try:
                consensus_index = consensus_ip_list.index(this_client[0])
                del consensus_ip_list[consensus_index]  # remove ip
                del consensus_opinion_list[consensus_index]  # remove ip's opinion
            except Exception as e:
                #logging.info( e
                logging.info(this_client.split(":")[0]+" not found in the consensus pool, won't remove")
            # remove from consensus

            logging.info("---thread "+str(threading.currentThread())+" ended---")
            #raise #test only
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
        logging.info("Server loop running in thread: "+ server_thread.name)

        #start connection manager
        t_manager = threading.Thread(target=manager())
        logging.info("Starting connection manager")
        t_manager.start()
        #start connection manager

        #server.serve_forever() #added
        server.shutdown()
        server.server_close()

    except Exception, e:
        logging.info("Node already running?")
        logging.info(e)
        #raise #only test
sys.exit()
