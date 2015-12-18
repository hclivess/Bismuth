import socket
import sys
import re
import ast
import sqlite3

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto import Random


# Create a TCP/IP socket
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

# Bind the socket to the port
server_address = ('localhost', 2829)
print 'starting up on %s port %s' % server_address
sock.bind(server_address)

# Listen for incoming connections
sock.listen(1)

#verify blockchain
con = None
db_i = 0

conn = sqlite3.connect('thincoin.db')
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS transactions (block_height, address, to_address, amount, signature, public_key)''')
c.execute("SELECT COALESCE(MAX(block_height)+1, 0) FROM transactions")
db_rows = c.fetchone()[0]
print db_rows

try:
    while db_rows > db_i:
        c.execute("SELECT * FROM transactions LIMIT 5 OFFSET '"+str(db_i)+"';")
        db_address = c.fetchone()[1]
        db_signature = c.fetchone()[4]
        db_public_key = c.fetchone()[5]
        
        print db_address
        print db_signature
        print db_public_key

        db_i = db_i + 1
    
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
    connection, client_address = sock.accept()

    talkie = 0
    try:
        print 'connection from', client_address

        # Receive and send data
        while True:
            hello = connection.recv(4096)
            print 'received '+ hello
            
            if hello:
                with open ("peers.txt", "r") as peer_list:
                    peers=peer_list.read()
                    print peers
                    connection.sendall(peers)
                    
            data = connection.recv(4096)
            if data:
                data_split = data.split(";")
                received_transaction = data_split[0]
                print "Received transaction: "+received_transaction
                #split message into values
                received_transaction_split = received_transaction.split(":")
                block_height = received_transaction_split[0]
                address = received_transaction_split[1]
                to_address = received_transaction_split[2]
                amount =received_transaction_split[3]
                #split message into values
                received_signature = data_split[1] #needs to be converted
                received_signature_tuple = ast.literal_eval(received_signature) #converting to tuple
                print "Received signature: "+received_signature
                received_public_key_readable = data_split[2]
                print "Received public key: "+received_public_key_readable

                #convert received strings
                received_public_key = RSA.importKey(received_public_key_readable)
                #convert received strings
                
                if received_public_key.verify(received_transaction, received_signature_tuple) == True:
                    print "The signature is valid"
                    #transaction processing
                    con = None
                    try:
                        conn = sqlite3.connect('thincoin.db')
                        c = conn.cursor()
                        #verify block
                        c.execute('''SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;''')
                        block_latest = c.fetchone()[0]
                        print "Latest block in db: "+block_latest
                        if int(block_height) != int(block_latest)+1:
                            print "Block height invalid"
                            #verify block
                        else:                      
                            #verify balance and blockchain                           
                            print "Verifying balance"
                            print address
                            c.execute("SELECT sum(amount) FROM transactions WHERE to_address = '"+address+"'")
                            inputs = c.fetchone()[0]
                            print "Total inputs: "+str(inputs)
                            c.execute("SELECT sum(amount) FROM transactions WHERE address = '"+address+"'")
                            outputs = c.fetchone()[0]
                            print "Total outputs: "+str(outputs)
                            if int(inputs) - int(outputs) - int(amount) < 0:
                                print "Your balance is too low for this transaction"
                            else:
                                print "Processing transaction"
                            #verify balance and blockchain                            
                                #execute transaction
                                c.execute("INSERT INTO transactions VALUES ('"+block_height+"','"+address+"','"+to_address+"','"+amount+"','"+received_signature+"','"+received_public_key_readable+"')") # Insert a row of data                    
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
                    #transaction processing
                else:
                    print "Signature invalid"

 
            else:
                print 'no more data from', client_address
                break
            
    finally:
        # Clean up the connection
        connection.close()
