from base64 import b64encode
import hashlib
import logging
import sqlite3
import time
import keys
import psutil
import pyping
import socket

from Crypto.Signature import PKCS1_v1_5 as PKCS
from Crypto.Hash import SHA
    
app_log = logging.getLoger("arches.log")

def sign_transaction(transaction, key):
    h = SHA.new(str(transaction))
    signer = PKCS.new(key)
    signature = signer.sign(h)
    encoded_signature = b64encode(signature)
    return signature, encoded_signature

def tx_to_mempool(timestamp, address, encoded_signature, pub_key_hash, openfield):
    cursor = None
    try:
        mempool = sqlite3.connect("mempool.db")
        mempool.text_factory = str
        cursor = mempool.cursor()
        
        m.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", (timestamp,
                                                                        address,
                                                                        address,
                                                                        '%.8f' % 0,
                                                                        encoded_signature,
                                                                        pub_key_hash,
                                                                        "0",
                                                                        str(openfield)))
        mempool.commit()
        app_log.info("Arches: Mempool updated with a transaction")
    except Exception as e:
        app_log.error("[{}] {} {}".format(time.time(),
                                          type(e),
                                          e))
    finally:
        if cursor is not None:
            cursor.close()
        mempool.close()

app_log.info("Arches: Mempool updated with a transaction")
if __name__ == "__main__":
    key, private_key_readable, public_key_readable, public_key_hashed, address = keys.read()

    timestamp = str(time.time())

    r = pyping.ping('google.com')                # Need to be root or

    openfield = "Arches:" + socket.gethostname(), psutil.virtual_memory(), psutil.cpu_times(), r.ret_code, r.destination, r.max_rtt, r.avg_rtt, r.min_rtt, r.destination_ip
    print(openfield)

    transaction = (timestamp, address, address, '%.8f' % 0, 0, str(openfield))  # this is signed
    print(transaction)

    signature, encoded_signature = sign_transaction(transaction)
    tx_to_mempool(timestamp, address, encoded_signature, pub_key_hash, openfield)
