import base64, os, getpass, hashlib
from Crypto import Random

try:
    from simplecrypt import decrypt
except ImportError:
    decrypt = None
from Crypto.PublicKey import RSA
import sys

def generate():
    # generate key pair and an address
    random_generator = Random.new().read
    key = RSA.generate(1024, random_generator)
    public_key = key.publickey()

    private_key_readable = key.exportKey().decode("utf-8")
    public_key_readable = key.publickey().exportKey().decode("utf-8")
    address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()  # hashed public key
    return private_key_readable, public_key_readable, address

def read():
    # import keys
    if not os.path.exists('privkey_encrypted.der'):
        # password = "" # Not used here
        key = RSA.importKey(open('privkey.der').read())
        private_key_readable = key.exportKey().decode("utf-8")
        # public_key = key.publickey()
    else:
        if not decrypt:
            print("Key decryption not available, install simplecrypt")
            sys.exit()
        password = getpass.getpass()
        encrypted_privkey = open('privkey_encrypted.der').read()
        decrypted_privkey = decrypt(password, base64.b64decode(encrypted_privkey))
        key = RSA.importKey(decrypted_privkey)  # be able to sign
        private_key_readable = key.exportKey().decode("utf-8")

    public_key_readable = open('pubkey.der').read()

    if (len(public_key_readable)) != 271:
        raise ValueError("Invalid public key length")

    public_key_hashed = base64.b64encode(public_key_readable.encode("utf-8")).decode("utf-8")
    address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()
    # import keys

    return key, private_key_readable, public_key_readable, public_key_hashed, address
