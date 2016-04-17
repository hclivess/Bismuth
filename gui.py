from datetime import datetime
import hashlib
import sqlite3
import socket
import time
import base64
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA

from Tkinter import *
root = Tk()

root.wm_title("[XBM] Bismuth")


# import keys
key = RSA.importKey(open('privkey.der').read())
public_key = key.publickey()
private_key_readable = str(key.exportKey())
public_key_readable = str(key.publickey().exportKey())
address = hashlib.sha224(public_key_readable).hexdigest()

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#s.settimeout(1)
try:
    s.connect(("127.0.0.1", int("2829"))) #connect to local node
except:
    print "Cannot connect to local node, please start it first"
    sys.exit(1)
print "Connected"

def table():
    # transaction table
    # data
    datasheet = ["time","from","to","amount"]

    conn = sqlite3.connect('ledger.db')
    c = conn.cursor()
    for row in c.execute("SELECT * FROM transactions WHERE address = '" + str(address) + "' OR to_address = '" + str(
            address) + "' ORDER BY block_height DESC LIMIT 19;"):
        db_timestamp = row[1]
        datasheet.append(datetime.fromtimestamp(float(db_timestamp)).strftime('%Y-%m-%d %H:%M:%S'))
        db_address = row[2]
        datasheet.append(db_address)
        db_to_address = row[3]
        datasheet.append(db_to_address)
        db_amount = row[4]
        datasheet.append(db_amount)
    conn.close()
    # data

    print datasheet

    k = 0
    for i in range(20):
        for j in range(4):
            e = Entry(f4,justify=RIGHT)
            e.grid(row=i+1, column=j, sticky=EW)
            e.insert(END,datasheet[k])

            k = k + 1


    # transaction table

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
    balance_msg = Label(f5, text = "Balance: "+str(balance))
    balance_msg.grid(row = 0, column = 0, sticky=E, padx = 15, pady=(15,0))

    spent_msg = Label(f5, text="Spent Total: " + str(debit))
    spent_msg.grid(row = 1, column = 0, sticky=E, padx = 15)

    received_msg = Label(f5, text="Received Total: " + str(credit))
    received_msg.grid(row = 2, column = 0, sticky=E, padx = 15)
    table()

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
    h = SHA.new(transaction)
    signer = PKCS1_v1_5.new(key)
    signature = signer.sign(h)
    signature_enc = base64.b64encode(signature)
    print "Client: Encoded Signature: "+str(signature_enc)

    verifier = PKCS1_v1_5.new(key)
    if verifier.verify(h, signature) == True:
        if int(amount_input) < 0:
            print "Client: Signature OK, but cannot use negative amounts"

        else:
            conn = sqlite3.connect('ledger.db')
            c = conn.cursor()
            c.execute("SELECT txhash FROM transactions ORDER BY block_height DESC LIMIT 1;")
            txhash = str(c.fetchone()[0])
            txhash_new = hashlib.sha224(str(transaction) + str(signature_enc) + str(txhash)).hexdigest() #define new tx hash based on previous #fix asap
            print "Client: New txhash to go with your transaction: "+txhash_new
            conn.close()

            print "Client: The signature and control txhash is valid, proceeding to send transaction, signature, new txhash and the public key"
            s.sendall("transaction")
            time.sleep(0.1)
            s.sendall(transaction+";"+str(signature_enc)+";"+public_key_readable+";"+str(txhash_new)) #todo send list
            time.sleep(0.1)
            balance_get()

    else:
        print "Client: Invalid signature"
    #enter transaction end


def node():
    print "Received node start command"


def app_quit():
    print "Received quit command"
    root.destroy()

#frames
f2 = Frame(root, height=100, width = 100)
f2.grid(row = 0, column = 1, sticky = W+E+N+S)

f3 = Frame(root, width = 500)
f3.grid(row = 0, column = 0, sticky = W+E+N+S)

f4 = Frame(root, height=100, width = 100)
f4.grid(row = 1, column = 0, sticky = W+E+N+S)

f5 = Frame(root, height=100, width = 100)
f5.grid(row = 1, column = 1, sticky = W+E+N+S)
#frames

#buttons

send_b = Button(f5, text="Send Bismuth", command=send, height=1, width=15)
send_b.grid(row=3, column=0, sticky=W+E+N+S, pady=(100, 4), padx=15)

start_b = Button(f5, text="Start node", command=node, height=1, width=15)
start_b.grid(row=4, column=0, sticky=W+E+N+S, pady=4,padx=15,columnspan=4)

balance_b = Button(f5, text="Check balance", command=balance_get, height=1, width=15)
balance_b.grid(row=5, column=0, sticky=W+E+N+S, pady=4,padx=15)

balance_b = Button(f5, text="Refresh table", command=table, height=1, width=15)
balance_b.grid(row=6, column=0, sticky=W+E+N+S, pady=4,padx=15)

quit_b = Button(f5, text="Quit", command=app_quit, height=1, width=15)
quit_b.grid(row=7, column=0, sticky=W+E+N+S, pady=4,padx=15)

#buttons

#address and amount
Label(f3, text="Your Address:", width=20).grid(row=0, pady=15)
gui_address = Entry(f3,width=57)
gui_address.grid(row=0,column=1)
gui_address.insert(0,address)

Label(f3, text="Recipient:", width=20).grid(row=1)
Label(f3, text="Amount:", width=20).grid(row=2)

to_address = Entry(f3, width=57)
to_address.grid(row=1, column=1, pady=15)

amount = Entry(f3, width=57)
amount.grid(row=2, column=1, pady=15)
amount.grid(row=2, column=1, pady=15)

balance_enumerator = Entry(f3, width=10)
#address and amount

Label(f4, text="Your latest transactions:", width=20).grid(row=0)

#logo
logo=PhotoImage(file="graphics/logo.gif")
image = Label(f2, image=logo)
image.grid(pady=5, padx=5)
#logo

balance_get() #get balance on start
root.mainloop()