import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from Crypto import Random
#from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
#from Crypto.Signature import PKCS1_v1_5
import hashlib
from multiprocessing import Process
from multiprocessing import freeze_support

desired = ["hclivess", "bismuth", "crypto"]
threads = 6

def search():
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
        while not any(x in address for x in desired):
            # generate key pair and an address
            key = RSA.generate(4096)
            public_key = key.publickey()

            private_key_readable = key.exportKey().decode("utf-8")
            public_key_readable = key.publickey().exportKey().decode("utf-8")
            address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()  # hashed public key

            app_log.info('Generating vanity attempt: ' + address)
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

if __name__ == '__main__':
    freeze_support() #must be this line, dont move ahead
    instances = range(threads)
    print(instances)
    for q in instances:
        p = Process(target=search, args=())
        p.start()
        print("thread {} started".format(p))

