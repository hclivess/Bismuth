from Tkinter import *
import time
import ast
import hashlib
import socket
import re
import sqlite3
import os
import sys
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto import Random
window = Tk()

def generate():
    if os.path.isfile("keys.pem") is True:
        print "keys.pem found"

    else:   
        #generate key pair and an address
        random_generator = Random.new().read
        key = RSA.generate(1024, random_generator)
        public_key = key.publickey()

        private_key_readable = str(key.exportKey())
        public_key_readable = str(key.publickey().exportKey())
        address = hashlib.sha224(public_key_readable).hexdigest() #hashed public key
        #generate key pair and an address

        print "Your address: "+ str(address)
        print "Your private key:\n "+ str(private_key_readable)
        print "Your public key:\n "+ str(public_key_readable)

        pem_file = open("keys.pem", 'a')
        pem_file.write(str(private_key_readable)+"\n"+str(public_key_readable) + "\n\n")
        pem_file.close()
        address_file = open ("address.txt", 'a')
        address_file.write(str(address)+"\n")
        address_file.close()


    # import keys
    key_file = open('keys.pem','r')
    key = RSA.importKey(key_file.read())
    public_key = key.publickey()
    private_key_readable = str(key.exportKey())
    public_key_readable = str(key.publickey().exportKey())
    address = hashlib.sha224(public_key_readable).hexdigest()

    print "Your address: "+ str(address)
    print "Your private key:\n "+ str(private_key_readable)
    print "Your public key:\n "+ str(public_key_readable)
    # import keys

    transaction = "1:genesis:"+address+":100000000"
    signature = key.sign(transaction, '')
    print "Signature: "+str(signature)
    txhash = hashlib.sha224(str(transaction) + str(signature) + str(public_key_readable)).hexdigest()
    print "Transaction Hash:" + txhash

    if os.path.isfile("ledger.db") is True:
        print "You are beyond genesis"
    else:
        #transaction processing
        con = None
        try:
            conn = sqlite3.connect('ledger.db')
            c = conn.cursor()
            c.execute("CREATE TABLE transactions (block_height INTEGER, address, to_address, amount, signature, public_key, txhash)")
            c.execute("INSERT INTO transactions VALUES ('1','genesis','"+address+"','100000000','"+str(signature)+"','"+public_key_readable+"','"+txhash+"')") # Insert a row of data                    
                   
            conn.commit() # Save (commit) the changes
            #todo: broadcast
            print "Genesis created, don't forget to hardcode your genesis address"
        except sqlite3.Error, e:                      
            print "Error %s:" % e.args[0]
            sys.exit(1)                        
        finally:                        
            if conn:
                conn.close()
        #transaction processing
    
def send():
    to_address_input = to_address_gui.get()
    #print to_address_input
    amount_input = amount_gui.get()
    #print amount_input
    
    if os.path.isfile("keys.pem") is True:
        print "keys.pem found"

    else:   
        #generate key pair and an address
        random_generator = Random.new().read
        key = RSA.generate(1024, random_generator)
        public_key = key.publickey()

        private_key_readable = str(key.exportKey())
        public_key_readable = str(key.publickey().exportKey())
        address = hashlib.sha224(public_key_readable).hexdigest() #hashed public key
        #generate key pair and an address

        print "Your address: "+ str(address)
        print "Your private key:\n "+ str(private_key_readable)
        print "Your public key:\n "+ str(public_key_readable)

        pem_file = open("keys.pem", 'a')
        pem_file.write(str(private_key_readable)+"\n"+str(public_key_readable) + "\n\n")
        pem_file.close()
        address_file = open ("address.txt", 'a')
        address_file.write(str(address)+"\n")
        address_file.close()


    # import keys
    key_file = open('keys.pem','r')
    key = RSA.importKey(key_file.read())
    public_key = key.publickey()
    private_key_readable = str(key.exportKey())
    public_key_readable = str(key.publickey().exportKey())
    address = hashlib.sha224(public_key_readable).hexdigest()

    print "Your address: "+ str(address)
    #print "Your private key:\n "+ str(private_key_readable)
    #print "Your public key:\n "+ str(public_key_readable)
    # import keys


    #open peerlist and connect
    with open ("peers.txt", "r") as peer_list:
        peers=peer_list.read()
        peer_tuples = re.findall ("'([\d\.]+)', '([\d]+)'",peers)
        print peer_tuples

    for tuple in peer_tuples:
        HOST = tuple[0]
        #print HOST
        PORT = int(tuple[1])
        #print PORT

     
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        #s.settimeout(5)
        s.connect((HOST, PORT))
        print "Connected to "+str(HOST)+" "+str(PORT)
        #network client program

        while True:

            s.sendall('Hello, server')
            time.sleep(0.5)

            peer = s.getpeername()
            
            data = s.recv(1024) #receive data, one and the only root point
            print 'Received data from '+ str(peer) +"\n"+ str(data)

            if data == "Peers":
                #get remote peers into tuples
                server_peer_tuples = re.findall ("'([\d\.]+)', '([\d]+)'",data)
                print server_peer_tuples
                print len(server_peer_tuples)
                #get remote peers into tuples

                #get local peers into tuples
                peer_file = open("peers.txt", 'r')
                peer_tuples = []
                for line in peer_file:
                    extension = re.findall ("'([\d\.]+)', '([\d]+)'",line)
                    peer_tuples.extend(extension)
                peer_file.close()
                print peer_tuples
                #get local peers into tuples

                for x in server_peer_tuples:
                    if x not in peer_tuples:
                        print str(x)+" is a new peer, saving."

                        peer_list_file = open("peers.txt", 'a')
                        peer_list_file.write(str(x)+"\n")
                        peer_list_file.close()        
                        
                    else:
                        print str(x)+" is not a new peer, skipping."

            if data == "Sync":            
                #sync start
                conn = sqlite3.connect('ledger.db')
                c = conn.cursor()                
                c.execute('SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1')
                db_txhash = c.fetchone()[0] #get latest txhash
                conn.close()
                print "txhash to send: " +str(db_txhash)
                
                s.sendall ("My latest txhash is as follows")
                time.sleep(0.1)
                s.sendall(db_txhash) #send latest txhash
                
                subdata = s.recv(1024)         

                if subdata == "Block not found":
                    print "Node didn't find the block, deleting latest entry"
                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()
                    c.execute('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1')
                    db_block_height = c.fetchone()[0]
                    c.execute('DELETE FROM transactions WHERE block_height ="'+str(db_block_height)+'"')
                    conn.commit()
                    conn.close()
                    
                    ##todo
                    
                       
                if subdata == "Block found":
                    ##todo delete all own followups
                   
                    print "Node has the block" #node should start sending txs in this step
                    #todo critical: make sure that received block height is correct
                    data = s.recv(1024)
                    #verify
                    sync_list = ast.literal_eval(data) #this is great, need to add it to client -> node sync
                    received_block_height = sync_list[0]
                    received_address = sync_list[1]
                    received_to_address = sync_list[2]
                    received_amount = sync_list [3]
                    received_signature = sync_list[4]
                    received_public_key_readable = sync_list[5]
                    received_public_key = RSA.importKey(sync_list[5])
                    received_txhash = sync_list[6]
                    received_transaction = str(received_address) +":"+ str(received_to_address) +":"+ str(received_amount) #todo: why not have bare list instead of converting?
                    received_signature_tuple = ast.literal_eval(received_signature) #converting to tuple

                    #txhash validation start

                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()
                    c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
                    txhash_db = c.fetchone()[0]

                    #delete all local followups
                    c.execute('DELETE FROM transactions WHERE block_height > "'+str(received_block_height)+'"')
                    conn.close()
                    #delete all local followups
                    
                    print "Last db txhash: "+str(txhash_db)
                    print "Received txhash: "+str(received_txhash)
                    print "Received transaction: "+str(received_transaction)

                    txhash_valid = 0
                    if received_txhash == hashlib.sha224(str(received_transaction) + str(received_signature) +str(txhash_db)).hexdigest(): #new hash = new tx + new sig + old txhash
                        print "txhash valid"
                        txhash_valid = 1

                        #update local db with received tx
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()
                        print "Verifying balance"
                        print "Received address: " +str(received_address)
                        c.execute("SELECT sum(amount) FROM transactions WHERE to_address = '"+received_address+"'")
                        credit = c.fetchone()[0]
                        c.execute("SELECT sum(amount) FROM transactions WHERE address = '"+received_address+"'")
                        debit = c.fetchone()[0]
                        if debit == None:
                            debit = 0
                        if credit == None:
                            credit = 0                                
                        print "Total credit: "+str(credit)                                
                        print "Total debit: "+str(debit)
                        balance = int(credit) - int(debit)
                        print "Transction address balance: "+str(balance)                       
                        conn.close()
                                
                        if  int(balance) - int(received_amount) < 0:
                            print "Their balance is too low for this transaction"
                        else:                              
                            #save step to db
                            conn = sqlite3.connect('ledger.db') 
                            c = conn.cursor()
                            c.execute("INSERT INTO transactions VALUES ('"+str(received_block_height)+"','"+str(received_address)+"','"+str(received_to_address)+"','"+str(received_amount)+"','"+str(received_signature)+"','"+str(received_public_key_readable)+"','"+str(received_txhash)+"')") # Insert a row of data
                            print "Ledger updated with a received transaction"
                            conn.commit() # Save (commit) the changes
                            conn.close()
                            #save step to db
                        print "Ledger synchronization finished"
                        #s.sendall("Sync finished")
                        #update local db with received tx                    


                        
                    else:
                        print "txhash invalid"
                        #rollback start
                        print "Received invalid txhash"
                        #rollback end
                        
                   
                    #txhash validation end

                if subdata == "No new blocks here":
                    print "We seem to be at the latest block"

                    #send tx
                    conn = sqlite3.connect('ledger.db')
                    c = conn.cursor()
                    c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
                    txhash = c.fetchone()[0]
                    conn.close()
                        
                    to_address = to_address_input
                    amount = amount_input
                    
                    transaction = str(address) +":"+ str(to_address) +":"+ str(amount)
                    signature = key.sign(transaction, '')
                    print "Signature: "+str(signature)

                    if public_key.verify(transaction, signature) == True:

             
                        conn = sqlite3.connect('ledger.db')
                        c = conn.cursor()
                        c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
                        txhash = str(c.fetchone()[0])
                        txhash_new = hashlib.sha224(str(transaction) + str(signature) + str(txhash)).hexdigest() #define new tx hash based on previous #fix asap
                        print "New txhash to go with your transaction: "+txhash_new
                        conn.close()
                           
                        print "The signature and control txhash is valid, proceeding to send transaction, signature, new txhash and the public key"
                        s.sendall("Transaction")
                        time.sleep(0.1)
                        s.sendall(transaction+";"+str(signature)+";"+public_key_readable+";"+str(txhash_new)) #todo send list

                        
                    else:
                        print "Invalid signature"
                    
                #broadcast
                #s.close()

                #network client program

def node():
    print "Received node start command"
        

def app_quit():
    print "Received quit command"
    window.destroy()

#address and amount
Label(window, text="To address", width=20).grid(row=0)
Label(window, text="Amount", width=20).grid(row=1)

to_address_gui = Entry(window, width=60)
to_address_gui.grid(row=0, column=1)

amount_gui = Entry(window, width=60)
amount_gui.grid(row=1, column=1)
#address and amount
    
#buttons
send_b = Button(window, text="Send transaction", command=send, height=1, width=15)
send_b.grid(row=3, column=0, sticky=W, pady=4)

start_b = Button(window, text="Start node", command=node, height=1, width=15)
start_b.grid(row=3, column=1, sticky=W, pady=4)

gen_b = Button(window, text="Address and keys", command=generate, height=1, width=15)
gen_b.grid(row=3, column=2, sticky=W, pady=4)

quit_b = Button(window, text="Quit", command=app_quit, height=1, width=15)
quit_b.grid(row=3, column=3, sticky=W, pady=4)
#buttons

#window.after(2000, task)
window.mainloop()
