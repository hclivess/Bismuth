import base64, os, hashlib
from Crypto.PublicKey import RSA

def read():
    if not os.path.exists('privkey_encrypted.der'):
        key = RSA.importKey(open('privkey.der').read())
        private_key_readable = str(key.exportKey())
        #public_key = key.publickey()
        encrypted = 0
        unlocked = 1
    else:
        encrypted = 1
        unlocked = 0

    #public_key_readable = str(key.publickey().exportKey())
    public_key_readable = open('pubkey.der').read()
    public_key_hashed = base64.b64encode(public_key_readable)
    address = hashlib.sha224(public_key_readable).hexdigest()

    return private_key_readable, encrypted, unlocked, address, public_key_readable, public_key_hashed