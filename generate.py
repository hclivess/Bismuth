import hashlib
import os

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto import Random

if os.path.isfile("keys.pem") is True:
    print "keys.pem already exists"

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
