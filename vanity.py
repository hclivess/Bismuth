import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from Crypto import Random
#from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
#from Crypto.Signature import PKCS1_v1_5
import hashlib

desired = "hcl"

log_formatter = logging.Formatter('%(asctime)s %(levelname)s %(funcName)s(%(lineno)d) %(message)s')
logFile = 'vanity.log'
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

# key maintenance
if os.path.isfile("privkey.der") is True:
    app_log.info("Client: privkey.der found")
else:
    address = ""
    while address.startswith(desired) != True:
        # generate key pair and an address
        random_generator = Random.new().read
        key = RSA.generate(1024, random_generator)
        public_key = key.publickey()

        private_key_readable = str(key.exportKey())
        public_key_readable = str(key.publickey().exportKey())
        address = hashlib.sha224(public_key_readable).hexdigest()  # hashed public key

        app_log.info('Generating vanity attempt: '+address)
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
