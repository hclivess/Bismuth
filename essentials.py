"""
Common helpers for Bismuth
"""
import os, db, sqlite3, hashlib, base64

# from Crypto import Random
from Crypto.PublicKey import RSA
from decimal import *
import re
import time

from quantizer import *

__version__ = "0.0.2"


def db_check(app_log):
    if not os.path.exists('backup.db'):
        # create empty backup file
        backup = sqlite3.connect('backup.db', timeout=1)
        backup.text_factory = str
        b = backup.cursor()
        db.execute(b, "CREATE TABLE IF NOT EXISTS transactions (block_height, timestamp, address, recipient, amount, signature, public_key, block_hash, fee, reward, operation, openfield)", app_log)
        db.commit(backup, app_log)
        db.execute(b, "CREATE TABLE IF NOT EXISTS misc (block_height, difficulty)", app_log)
        db.commit(backup, app_log)
        app_log.warning("Created backup file")
        backup.close()
        # create empty backup file

    """
    # Now done via mempool module
    if not os.path.exists('mempool.db'):
        # create empty mempool
        mempool = sqlite3.connect('mempool.db', timeout=1)
        mempool.text_factory = str
        m = mempool.cursor()
        db.execute(m, ("CREATE TABLE IF NOT EXISTS transactions (timestamp, address, recipient, amount, signature, public_key, operation, openfield)"), app_log)
        db.commit(mempool, app_log)
        app_log.warning("Created mempool file")
        mempool.close()
        # create empty mempool
    """


def keys_check(app_log):
    # key maintenance
    if os.path.isfile("privkey.der") is True:
        app_log.warning("privkey.der found")
    elif os.path.isfile("privkey_encrypted.der") is True:
        app_log.warning("privkey_encrypted.der found")
    else:
        while True:
            # generate key pair and an address
            key = RSA.generate(4096)
            #public_key = key.publickey()

            private_key_readable = key.exportKey().decode("utf-8")
            public_key_readable = key.publickey().exportKey().decode("utf-8")
            address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()  # hashed public key
            # generate key pair and an address

            app_log.info("Your address: {}".format(address))
            app_log.info("Your public key: {}".format(public_key_readable))

            with open ("privkey.der", 'w') as privkey_file:
                privkey_file.write(str(private_key_readable))

            with open ("pubkey.der", 'w') as pubkey_file:
                pubkey_file.write(str(public_key_readable))

            # verify
            key_test = RSA.importKey (open ("privkey.der").read ())
            public_key_test = open ("pubkey.der".encode ('utf-8')).read()

            if key_test.publickey().exportKey().decode("utf-8") != public_key_test:
                app_log.warning("Keys do not match, recreating")
            else:
                app_log.warning ("Keys match, continuing")
                break

            # verify


def keys_load(privkey, pubkey):
    # print ("loaded",privkey, pubkey)
    # import keys
    try:  # unencrypted
        key = RSA.importKey(open(privkey).read())
        private_key_readable = key.exportKey().decode("utf-8")
        # public_key = key.publickey()
        encrypted = False
        unlocked = True

    except:  # encrypted
        encrypted = True
        unlocked = False
        key = None
        private_key_readable = None

    # public_key_readable = str(key.publickey().exportKey())
    public_key_readable = open(pubkey.encode('utf-8')).read()

    if (len(public_key_readable)) != 271 and (len(public_key_readable)) != 799:
        raise ValueError("Invalid public key length: {}".format(len(public_key_readable)))

    public_key_hashed = base64.b64encode(public_key_readable.encode('utf-8'))
    myaddress = hashlib.sha224(public_key_readable.encode('utf-8')).hexdigest()

    return key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_hashed, myaddress


# Dup code, not pretty, but would need address module to avoid dup
def address_validate(address):
    return re.match('[abcdef0123456789]{56}', address)


# Dup code, not pretty, but would need address module to avoid dup
def validate_pem(public_key):
    # verify pem as cryptodome does
    pem_data = base64.b64decode(public_key).decode("utf-8")
    regex = re.compile("\s*-----BEGIN (.*)-----\s+")
    match = regex.match(pem_data)
    if not match:
        raise ValueError("Not a valid PEM pre boundary")
    marker = match.group(1)
    regex = re.compile("-----END (.*)-----\s*$")
    match = regex.search(pem_data)
    if not match or match.group(1) != marker:
        raise ValueError("Not a valid PEM post boundary")
        # verify pem as cryptodome does


def fee_calculate(openfield):
    fee = Decimal("0.01") + (Decimal(len(openfield)) / Decimal("100000"))  # 0.01 dust
    if "token:issue:" in openfield:
        fee = Decimal(fee) + Decimal("10")
    if "alias=" in openfield:
        fee = Decimal(fee) + Decimal("1")
    return quantize_eight(fee)


def execute_param_c(cursor, query, param, app_log):
    """Secure execute w/ param for slow nodes"""
    while True:
        try:
            cursor.execute(query, param)
            break
        except Exception as e:
            app_log.warning("Database query: {} {} {}".format(cursor, query, param))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(0.1)
    return cursor
