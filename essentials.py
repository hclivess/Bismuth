"""
Common helpers for Bismuth
"""
import os, db, sqlite3, hashlib, base64

# from Crypto import Random
from Cryptodome.PublicKey import RSA
import getpass
import re
import time
import json
from simplecrypt import *

from quantizer import *

__version__ = "0.0.3"


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


def sign_rsa(timestamp, address, recipient, amount, operation, openfield, key, public_key_hashed):
    from Cryptodome.Signature import PKCS1_v1_5
    from Cryptodome.Hash import SHA

    if not key:
        raise BaseException("The wallet is locked, you need to provide a decrypted key")

    transaction = (str (timestamp), str (address), str (recipient), '%.8f' % float (amount), str (operation), str (openfield))  # this is signed, float kept for compatibility

    h = SHA.new (str(transaction).encode())
    signer = PKCS1_v1_5.new (key)
    signature = signer.sign (h)
    signature_enc = base64.b64encode(signature)

    verifier = PKCS1_v1_5.new (key)
    if verifier.verify (h, signature):
        return_value = str (timestamp), str (address), str (recipient), '%.8f' % float (amount), str (signature_enc.decode ("utf-8")), str (public_key_hashed.decode ("utf-8")), str (operation), str (openfield)  # float kept for compatibility
    else:
        return_value = False

    return return_value


def keys_check(app_log, keyfile):
    # key maintenance
    if os.path.isfile("privkey.der") is True:
        app_log.warning("privkey.der found")
    elif os.path.isfile("privkey_encrypted.der") is True:
        app_log.warning("privkey_encrypted.der found")
        os.rename("privkey_encrypted.der","privkey.der")

    elif os.path.isfile (keyfile) is True:
        app_log.warning ("{} found".format(keyfile))
    else:
        # generate key pair and an address
        key = RSA.generate(4096)
        #public_key = key.publickey()

        private_key_readable = key.exportKey().decode("utf-8")
        public_key_readable = key.publickey().exportKey().decode("utf-8")
        address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()  # hashed public key
        # generate key pair and an address

        app_log.info("Your address: {}".format(address))
        app_log.info("Your public key: {}".format(public_key_readable))

        # export to single file
        keys_save(private_key_readable, public_key_readable, address, keyfile)
        # export to single file


def keys_save(private_key_readable, public_key_readable, address, file):
    wallet_dict = {}
    wallet_dict['Private Key'] = private_key_readable
    wallet_dict['Public Key'] = public_key_readable
    wallet_dict['Address'] = address

    if not isinstance(file,str):
        file = file.name

    with open (file, 'w') as keyfile:
        json.dump (wallet_dict, keyfile)

        
def keys_load(privkey="privkey.der", pubkey="pubkey.der"):
    keyfile = "wallet.der"
    if os.path.exists("wallet.der"):
        print("Using modern wallet method")
        return keys_load_new ("wallet.der")

    else:
        # print ("loaded",privkey, pubkey)
        # import keys
        try:  # unencrypted
            key = RSA.importKey(open(privkey).read())
            private_key_readable = key.exportKey ().decode ("utf-8")
            # public_key = key.publickey()
            encrypted = False
            unlocked = True

        except:  # encrypted
            encrypted = True
            unlocked = False
            key = None
            private_key_readable = open(privkey).read()

        # public_key_readable = str(key.publickey().exportKey())
        public_key_readable = open(pubkey.encode('utf-8')).read()

        if (len(public_key_readable)) != 271 and (len(public_key_readable)) != 799:
            raise ValueError("Invalid public key length: {}".format(len(public_key_readable)))

        public_key_hashed = base64.b64encode(public_key_readable.encode('utf-8'))
        address = hashlib.sha224(public_key_readable.encode('utf-8')).hexdigest()

        print("Upgrading wallet")
        keys_save (private_key_readable, public_key_readable, address, keyfile)

        return key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_hashed, address, keyfile


def keys_unlock(private_key_encrypted):
    password = getpass.getpass ()
    encrypted_privkey = private_key_encrypted
    decrypted_privkey = decrypt (password, base64.b64decode (encrypted_privkey))
    key = RSA.importKey (decrypted_privkey)  # be able to sign
    private_key_readable = key.exportKey ().decode ("utf-8")
    return key, private_key_readable


def keys_load_new(keyfile="wallet.der"):
    # import keys

    with open (keyfile, 'r') as keyfile:
        wallet_dict = json.load (keyfile)

    private_key_readable = wallet_dict['Private Key']
    public_key_readable = wallet_dict['Public Key']
    address = wallet_dict['Address']

    try:  # unencrypted
        key = RSA.importKey(private_key_readable)
        encrypted = False
        unlocked = True

    except:  # encrypted
        encrypted = True
        unlocked = False
        key = None

    # public_key_readable = str(key.publickey().exportKey())
    if (len(public_key_readable)) != 271 and (len(public_key_readable)) != 799:
        raise ValueError("Invalid public key length: {}".format(len(public_key_readable)))

    public_key_hashed = base64.b64encode(public_key_readable.encode('utf-8'))

    return key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_hashed, address, keyfile


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


def fee_calculate(openfield, operation='', block=0):
    # block var will be removed after HF
    fee = Decimal("0.01") + (Decimal(len(openfield)) / Decimal("100000"))  # 0.01 dust
    if operation == "token:issue":
        fee = Decimal(fee) + Decimal("10")
    if openfield.startswith("alias="):
        fee = Decimal(fee) + Decimal("1")
    return quantize_eight(fee)


def execute_param_c(cursor, query, param, app_log):
    """Secure execute w/ param for slow nodes"""
    while True:
        try:
            cursor.execute(query, param)
            break
        except UnicodeEncodeError as e:
            app_log.warning("Database query: {} {} {}".format(cursor, query, param))
            app_log.warning("Database skip reason: {}".format(e))
            break
        except Exception as e:
            app_log.warning("Database query: {} {} {}".format(cursor, query, param))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(0.1)
    return cursor


def is_sequence(arg):
    return (not hasattr(arg, "strip") and
            hasattr(arg, "__getitem__") or
            hasattr(arg, "__iter__"))
