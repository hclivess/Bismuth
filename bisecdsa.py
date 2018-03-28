from ecdsa import SigningKey,SECP256k1,VerifyingKey
from hashlib import blake2b
import os

def keys_load():
    privkey_file = open("privkey_ecdsa.pem", 'rb').read()
    privkey = SigningKey.from_string(privkey_file, curve=SECP256k1)

    pubkey_file = open("pubkey_ecdsa.pem", 'rb').read()
    pubkey = VerifyingKey.from_string(pubkey_file, curve=SECP256k1)

    return privkey, pubkey

def keys_save(key, file):
    print(key)
    open(file, 'wb').write(key)
    return True

def privkey_generate():
    if not os.path.exists("privkey_ecdsa.pem"):
        print ("generating keys")
        privkey_raw = SigningKey.generate(curve=SECP256k1)

        privkey=privkey_raw.to_string()
        keys_save(privkey,"privkey_ecdsa.pem")

        pubkey=privkey_raw.get_verifying_key().to_string()
        keys_save(pubkey, "pubkey_ecdsa.pem")

        address = blake2b(privkey, digest_size=20)
        print(address.hexdigest())

def sign(message, privkey, pubkey):
    message = message.encode()
    signature = privkey.sign(message)
    assert pubkey.verify(signature, message)

privkey, pubkey = keys_load()
print (privkey.to_string(), pubkey.to_string())

privkey_generate()

sign("message",privkey, pubkey)

