#todo : node needs to broadcast to other nodes so there is no fork in case of malicious send client
import hashlib
import socket
import sys
import re
import ast
import sqlite3
import time
import requests
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto import Random

port = int(2829)
#"""
#connectivity to self node

try:    
    r = requests.get(r'http://jsonip.com')
    ip= r.json()['ip']
    print 'Your IP is', ip
except:
    pass
sock_self = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock_self.settimeout(3)
result = sock_self.connect_ex((ip,port))
sock_self.close()
#result = 0 #enable for test
if result == 0:
    print "Port is open"   
#get local peers into tuples
    peer_file = open("peers.txt", 'r')
    peer_tuples = []
    for line in peer_file:
        extension = re.findall ("'([\d\.]+)', '([\d]+)'",line)
        peer_tuples.extend(extension)
    peer_file.close()
    peer_me = ("('"+str(ip)+"', '"+str(port)+"')")
    if peer_me not in str(peer_tuples): #stringing tuple is a nasty way
        peer_list_file = open("peers.txt", 'a')
        peer_list_file.write((peer_me)+"\n")
        print "Local node saved to peer file"
        peer_list_file.close()
    else:
        print "Self node already saved"
        
#get local peers into tuples    
else:
   print "Port is not open"
#connectivity to self node
#"""
   
# Create a TCP/IP socket
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

# Bind the socket to the port
server_address = ('localhost', port)
print 'starting up on %s port %s' % server_address
sock.bind(server_address)

# Listen for incoming connections
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.listen(1)

#verify blockchain
con = None
conn = sqlite3.connect('ledger.db')
c = conn.cursor()
#c.execute("CREATE TABLE IF NOT EXISTS transactions (block_height, address, to_address, amount, signature, public_key)")
c.execute("SELECT Count(*) FROM transactions")
db_rows = c.fetchone()[0]
print "Total steps: "+str(db_rows)

#verify genesis
c.execute("SELECT * FROM transactions ORDER BY block_height ASC LIMIT 1")
genesis = c.fetchone()[2]
print "Genesis: "+genesis
if str(genesis) != "b813b03700c22478d7480cd6810a85dd704acec9030f587c5d8ed0f6": #change this line to your genesis address if you want to clone
    print "Invalid genesis address"
    sys.exit(1)
#verify genesis

try:
    for row in c.execute('SELECT * FROM transactions ORDER BY block_height'):
        db_block_height = row[0]
        db_address = row[1]
        db_to_address = row[2]
        db_amount = row [3]
        db_signature = row[4]
        db_public_key = RSA.importKey(row[5])
        db_txhash = row[6]
        db_transaction = str(db_block_height) +":"+ str(db_address) +":"+ str(db_to_address) +":"+ str(db_amount) 

        #print db_transaction

        db_signature_tuple = ast.literal_eval(db_signature) #converting to tuple
        
        if db_public_key.verify(db_transaction, db_signature_tuple) == True:
            print "Step "+str(db_block_height)+" is valid"
        else:
            print "Step "+str(db_block_height)+" is invalid"
            if db_block_height == str(1):
                print "Your genesis signature is invalid, someone meddled with the database"
                sys.exit(1)
        
except sqlite3.Error, e:                        
    print "Error %s:" % e.args[0]
    sys.exit(1)                        
finally:                        
    if conn:
        conn.close()
#verify blockchain

while True:
    # Wait for a connection
    print 'waiting for a connection'
    
    
    try:
        connection, client_address = sock.accept()
        print 'connection from', client_address
### LOCAL CHECKS FINISHED ###
        
        # Receive and send data
        while True:
            
            hello = connection.recv(4096)
            #hello message
            print 'Received: '+ hello
            
            if hello == 'Hello, server':
                with open ("peers.txt", "r") as peer_list:
                    peers=peer_list.read()
                    print peers
                    connection.sendall(peers)
            #hello message                    

                
            #send sync data to client
            sync = connection.recv(4096)
            if sync == "Block height":
                sync = connection.recv(4096)
                print "Received: Client is at block: "+(sync)

                #latest local block
                #sync = 1 #pretend desync for TEST PURPOSES, client block no. x
                
                try:
                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()
                    c.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
                    block_latest = c.fetchone()[0]
                    print "Latest block in db: "+str(block_latest)
                    if int(sync) < block_latest:
                        print "Client is not up to date, sending new blocks"
                        #calculate sync data
                        block_difference = abs(int(sync) - int(block_latest))
                        print "Sending "+str(block_difference)+" blocks"
                        #calcualte sync data
                        connection.sendall(str(block_difference)) #inform the client how much data he will receive
                        
                        for row in c.execute("SELECT * FROM transactions ORDER BY block_height ASC LIMIT '"+str(sync)+"','"+str(block_difference)+"';"):
                            time.sleep(0.1)
                            connection.sendall(str(row)) #send data
                        print "All new transactions sent to client"
                        
                    else:
                        print "Client is up to date"
                        connection.sendall("No new blocks here")
                except sqlite3.Error, e:
                    print "Error %s:" % e.args[0]
                    sys.exit(1)                        
                finally:                        
                    if conn:
                        conn.close()
                #latest local block

            #rollback start
            rollback_hash = connection.recv(4096) #todo unify by removing this
            if rollback_hash == "Invalid txhash":
                rollback_hash = connection.recv(4096) #received client's latest synched hash, find it in the database and send followup txs; if not found, send info and wait for regression

                conn = sqlite3.connect('ledger.db')
                c = conn.cursor()
                c.execute("SELECT block_height FROM transactions WHERE txhash='"+rollback_hash+"';") #todo select a range using limit and offset
                try:
                    block_height_sync = c.fetchone()[0]
                    print "Client's last block valid in our database is at the following height: "+str(block_height_sync) #now we should send it to the client which should delete all transactions following this block height
                    connection.sendall("Block found")
                    connection.sendall(block_height_sync) #client must delete all txs following this one from their db

                    for row in c.execute('SELECT * FROM transactions ORDER BY block_height WHERE block_height => "'+block_height_sync+'" ')
                        followup_tx = str(row)
                        print followup_tx
                        #send all followup txs
                    connection.sendall("No more blocks")

                except:
                    print "Client's block not found in local database"
                    #request a -1 hash from client to seek it in the local database
                    connection.sendall("Block not found")
                
            #rollback end    

            
            data = connection.recv(4096)             
            if data == "Transaction":
                data = connection.recv(4096)
                data_split = data.split(";")
                received_transaction = data_split[0]
                print "Received transaction: "+received_transaction
                #split message into values
                try:
                    received_transaction_split = received_transaction.split(":")
                    block_height = int(received_transaction_split[0])
                    address = received_transaction_split[1]
                    to_address = received_transaction_split[2]
                    amount = int(received_transaction_split[3])
                except Exception as e:
                    print "Something wrong with the transaction ("+str(e)+")"
                    break
                #split message into values
                received_signature = data_split[1] #needs to be converted
                received_signature_tuple = ast.literal_eval(received_signature) #converting to tuple
                print "Received signature: "+received_signature
                received_public_key_readable = data_split[2]
                print "Received public key: "+received_public_key_readable
                received_txhash = data_split[3]

                #convert received strings
                received_public_key = RSA.importKey(received_public_key_readable)
                #convert received strings
                
                if received_public_key.verify(received_transaction, received_signature_tuple) == True:
                    print "The signature is valid"
                    #transaction processing
                    con = None
                    try:
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()
                        #verify block
                        c.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
                        block_latest = c.fetchone()[0]
                        print "Latest block in db: "+str(block_latest)
                        if int(block_height) != int(block_latest)+1:
                            print "Block height invalid"
                            #verify block
                        else:                      
                            #verify balance and blockchain                           
                            print "Verifying balance"
                            print address
                            c.execute("SELECT sum(amount) FROM transactions WHERE to_address = '"+address+"'")
                            credit = c.fetchone()[0]

                            c.execute("SELECT sum(amount) FROM transactions WHERE address = '"+address+"'")
                            debit = c.fetchone()[0]
                            if debit == None:
                                debit = 0
                            if credit == None:
                                credit = 0                                
                            print "Total credit: "+str(credit)                                
                            print "Total debit: "+str(debit)
                            balance = int(credit) - int(debit)
                            print "Your balance: "+str(balance) 

                            if  int(balance) - int(amount) < 0:
                                print "Your balance is too low for this transaction"

                           
                            else:
                                print "Processing transaction"

                            #txhash verification here TODO
                            #new hash = new tx + new sig + old txhash
                            try:
                                for row in c.execute('SELECT * FROM transactions ORDER BY block_height'):
                                    txhash = row[6]
                            except sqlite3.Error, e:                        
                                print "Error %s:" % e.args[0]
                                sys.exit(1)                                                        

                            if received_txhash == hashlib.sha224(str(received_transaction) + str(received_signature) +str(txhash)).hexdigest(): #new hash = new tx + new sig + old txhash
                                print "txhash valid"
                                txhash_valid = 1
                            else:
                                print "txhash invalid"
                                break
                                                            
                            #verify balance and blockchain                            
                                #execute transaction
                                
                            c.execute("INSERT INTO transactions VALUES ('"+str(block_height)+"','"+str(address)+"','"+str(to_address)+"','"+str(amount)+"','"+str(received_signature)+"','"+str(received_public_key_readable)+"','"+str(received_txhash)+"')") # Insert a row of data                    
                            #execute transaction                                
                            conn.commit() # Save (commit) the changes
                            #todo: broadcast
                            print "Saved"
                    except sqlite3.Error, e:                      
                        print "Error %s:" % e.args[0]
                        sys.exit(1)                        
                    finally:                        
                        if conn:
                            conn.close()
                            print "Database closed"
                            
                    #transaction processing
                else:
                    print "Signature invalid"

            else:
                print 'no more data from', client_address
                break
            
    finally:
        # Clean up the connection
        if connection:
            connection.close()
