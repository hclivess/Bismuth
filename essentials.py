import os, db, sqlite3, hashlib, base64

from Crypto.PublicKey import RSA

def db_check(app_log):
    if not os.path.exists('backup.db'):
        # create empty backup file
        backup = sqlite3.connect('backup.db', timeout=1)
        backup.text_factory = str
        b = backup.cursor()
        db.execute(b, ("CREATE TABLE IF NOT EXISTS transactions (block_height, timestamp, address, recipient, amount, signature, public_key, block_hash, fee, reward, operation, openfield)"), app_log)
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
        db.execute(m, ("CREATE TABLE IF NOT EXISTS transactions (timestamp, address, recipient, amount, signature, public_key, operation, openfield)"), app_log)
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
    try: #unencrypted
        key = RSA.importKey(open(privkey).read())
        private_key_readable = key.exportKey().decode("utf-8")
        # public_key = key.publickey()
        encrypted = False
        unlocked = True

    except: #encrypted
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