import os, db, sqlite3, hashlib

from Crypto import Random
from Crypto.PublicKey import RSA

def db_check(app_log):
    if not os.path.exists('backup.db'):
        # create empty backup file
        backup = sqlite3.connect('backup.db', timeout=1)
        backup.text_factory = str
        b = backup.cursor()
        db.execute(b, ("CREATE TABLE IF NOT EXISTS transactions (block_height, timestamp, address, recipient, amount, signature, public_key, block_hash, fee, reward, keep, openfield)"), app_log)
        db.commit(backup, app_log)
        db.execute(b, ("CREATE TABLE IF NOT EXISTS misc (block_height, difficulty)"), app_log)
        db.commit(backup, app_log)
        app_log.warning("Created backup file")
        backup.close()
        # create empty backup file

    if not os.path.exists('mempool.db'):
        # create empty mempool
        mempool = sqlite3.connect('mempool.db', timeout=1)
        mempool.text_factory = str
        m = mempool.cursor()
        db.execute(m, ("CREATE TABLE IF NOT EXISTS transactions (timestamp, address, recipient, amount, signature, public_key, keep, openfield)"), app_log)
        db.commit(mempool, app_log)
        app_log.warning("Created mempool file")
        mempool.close()
        # create empty mempool

def keys_check(app_log):
    # key maintenance
    if os.path.isfile("privkey.der") is True:
        app_log.warning("privkey.der found")
    elif os.path.isfile("privkey_encrypted.der") is True:
        app_log.warning("privkey_encrypted.der found")
    else:
        # generate key pair and an address
        random_generator = Random.new().read
        key = RSA.generate(4096, random_generator)
        public_key = key.publickey()

        private_key_readable = key.exportKey().decode("utf-8")
        public_key_readable = key.publickey().exportKey().decode("utf-8")
        address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()  # hashed public key
        # generate key pair and an address

        app_log.info("Your address: {}".format(address))
        app_log.info("Your public key: {}".format(public_key_readable))

        pem_file = open("privkey.der", 'a')
        pem_file.write(str(private_key_readable))
        pem_file.close()

        pem_file = open("pubkey.der", 'a')
        pem_file.write(str(public_key_readable))
        pem_file.close()

        address_file = open("address.txt", 'a')
        address_file.write(str(address) + "\n")
        address_file.close()

