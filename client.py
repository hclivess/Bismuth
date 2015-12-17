import hashlib
import socket
import re
import sqlite3

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto import Random


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


#network client program
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#open peerlist and connect
with open ("peers.txt", "r") as peer_list:
    peers=peer_list.read()
    peer_tuples = re.findall ("'([\d\.]+)', '([\d]+)'",peers)
    print peer_tuples

connected = 0
while connected == 0:
    for tuple in peer_tuples:
        HOST = tuple[0]
        #print HOST
        PORT = int(tuple[1])
        #print PORT

        try:
            s.connect((HOST, PORT))
            connected = 1
            print "Connected to "+str(HOST)+" "+str(PORT)
            break
        except:
            print "Cannot connect to "+str(HOST)+" "+str(PORT)
            pass

#open peerlist and connect


s.sendall('Hello, server')


peer = s.getpeername()
data = s.recv(1024) #receive data
print 'Received data from '+ str(peer) +"\n"+ str(data)

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
        peer_list_file.write(x+"\n")
        peer_list_file.close()        
        
    else:
        print str(x)+" is not a new peer, skipping."


#network client program

#playground
con = None
try:
    conn = sqlite3.connect('thincoin.db')
    c = conn.cursor()
    c.execute('''SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;''')
    block_height = int(c.fetchone()[0])+1
except sqlite3.Error, e:                        
    print "Error %s:" % e.args[0]
    sys.exit(1)                        
finally:                        
    if conn:
        conn.close()       

to_address = "dummy2"
amount = 3

transaction = ""+str(block_height) +":"+ str(address) +":"+ str(to_address) +":"+ str(amount)
signature = key.sign(transaction, '')
print "Signature: "+str(signature)

if public_key.verify(transaction, signature) == True:
    print "The signature is valid, proceeding to send transaction, signature and the public key"
    s.sendall(transaction+";"+str(signature)+";"+public_key_readable) #need to resolve how to convert back to format in server
else:
    print "Invalid signature"
#playground

s.close()
