import hashlib
import sqlite3
import socket
import time
from Crypto.PublicKey import RSA

from Tkinter import *
window = Tk()
window.wm_title("[XCO] CODE")

# import keys
key_file = open('keys.pem','r')
key = RSA.importKey(key_file.read())
public_key = key.publickey()
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
address = hashlib.sha224(public_key_readable).hexdigest()

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#s.settimeout(1)
try:
    s.connect(("127.0.0.1", int("2829")))
except:
    print "Cannot connect to local node, please start it first"
    sys.exit(1)
print "Connected"

def balance_get():
    conn = sqlite3.connect('ledger.db')
    c = conn.cursor()
    c.execute("SELECT sum(amount) FROM transactions WHERE to_address = '"+address+"'")
    credit = c.fetchone()[0]
    c.execute("SELECT sum(amount) FROM transactions WHERE address = '"+address+"'")
    debit = c.fetchone()[0]
    if debit == None:
        debit = 0
    if credit == None:
        credit = 0
    balance = credit - debit
    print "Node: Transction address balance: "+str(balance)                       
    conn.close()
    #updata balance label
    balance_msg = Label(window, text = "Balance: "+str(balance))
    balance_msg.grid(row = 3, column = 1, columnspan = 3)

def send():
    print "Received tx command"
    to_address_input = to_address.get()
    print to_address_input
    amount_input = amount.get()
    print amount_input
    #enter transaction start
    conn = sqlite3.connect('ledger.db')
    c = conn.cursor()
    c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
    txhash = c.fetchone()[0]
    conn.close()
        
    timestamp = str(time.time())

    transaction = str(timestamp) +":"+ str(address) +":"+ str(to_address_input) +":"+ str(amount_input)
    signature = key.sign(transaction, '')
    print "Client: Signature: "+str(signature)

    if public_key.verify(transaction, signature) == True:
        if int(amount_input) < 0:
            print "Client: Signature OK, but cannot use negative amounts"

        else:
            conn = sqlite3.connect('ledger.db')
            c = conn.cursor()
            c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
            txhash = str(c.fetchone()[0])
            txhash_new = hashlib.sha224(str(transaction) + str(signature) + str(txhash)).hexdigest() #define new tx hash based on previous #fix asap
            print "Client: New txhash to go with your transaction: "+txhash_new
            conn.close()
               
            print "Client: The signature and control txhash is valid, proceeding to send transaction, signature, new txhash and the public key"
            s.sendall("transaction")
            time.sleep(0.1)
            s.sendall(transaction+";"+str(signature)+";"+public_key_readable+";"+str(txhash_new)) #todo send list
            time.sleep(0.1)
        
    else:
        print "Client: Invalid signature"
    #enter transaction end
    

def node():
    print "Received node start command"
    

def app_quit():
    print "Received quit command"
    window.destroy()

balance_get() #get balance on start

#address and amount
Label(window, text="To address", width=20).grid(row=0)
Label(window, text="Amount", width=20).grid(row=1)

to_address = Entry(window, width=30)
to_address.grid(row=0, column=1)

amount = Entry(window, width=30)
amount.grid(row=1, column=1)

balance_enumerator = Entry(window, width=10)
#address and amount

#buttons

send_b = Button(window, text="Send transaction", command=send, height=1, width=15)
send_b.grid(row=4, column=1, sticky=W, pady=4)

start_b = Button(window, text="Start node", command=node, height=1, width=15)
start_b.grid(row=5, column=1, sticky=W, pady=4)

balance_b = Button(window, text="Check balance", command=balance_get, height=1, width=15)
balance_b.grid(row=6, column=1, sticky=W, pady=4)

quit_b = Button(window, text="Quit", command=app_quit, height=1, width=15)
quit_b.grid(row=7, column=1, sticky=W, pady=4)

#buttons

mainloop()
