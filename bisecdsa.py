from ecdsa import SigningKey,SECP256k1,VerifyingKey, BadSignatureError
from hashlib import blake2b
import os

def keys_load(privkey_file, pubkey_file):
    privkey_loaded = open(privkey_file, 'rb').read()
    privkey = SigningKey.from_string(privkey_loaded, curve=SECP256k1)

    pubkey_loaded = open(pubkey_file, 'rb').read()
    pubkey = VerifyingKey.from_string(pubkey_loaded, curve=SECP256k1)

    address = blake2b(privkey.to_string(), digest_size=20).hexdigest()

    return privkey, pubkey, address

def keys_save(key, file):
    print(key)
    open(file, 'wb').write(key)
    return True

def privkey_generate(privkey_file, pubkey_file):
    if not os.path.exists(privkey_file):
        print ("generating keys")
        privkey_raw = SigningKey.generate(curve=SECP256k1)

        privkey=privkey_raw.to_string()
        keys_save(privkey,privkey_file)

        pubkey=privkey_raw.get_verifying_key().to_string()
        keys_save(pubkey,pubkey_file)


def sign(message, privkey, pubkey):
    message = message.encode()
    signature = privkey.sign(message)
    assert pubkey.verify(signature, message)
    return signature

def verify(pubkey, message, signature):
    message = message.encode()
    try:
        pubkey.verify(signature, message)
        print("Valid signature")
    except BadSignatureError:
        print("Invalid signature")


if __name__ == "__main__":
    privkey_generate("privkey_ecdsa.pem","pubkey_ecdsa.pem")
    privkey, pubkey, address = keys_load("privkey_ecdsa.pem","pubkey_ecdsa.pem")
    print (address)

    message = "message"
    signature = sign(message,privkey, pubkey)
    sign("message",privkey, pubkey)
    verify(pubkey,message,signature)


