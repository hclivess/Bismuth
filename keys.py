import base64, hashlib, json
from Cryptodome.PublicKey import RSA

def generate():
    # generate key pair and an address
    key = RSA.generate(4096)

    private_key_readable = key.exportKey().decode("utf-8")
    public_key_readable = key.publickey().exportKey().decode("utf-8")
    address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()  # hashed public key
    return private_key_readable, public_key_readable, address

def read():
    # import keys
    with open ("wallet.der", 'r') as wallet_file:
            wallet_dict = json.load (wallet_file)
    private_key_readable = wallet_dict['Private Key']
    public_key_readable = wallet_dict['Public Key']
    key = private_key_readable

    if (len(public_key_readable)) != 271 and (len(public_key_readable)) != 799:
        raise ValueError("Invalid public key length: {}".format(len(public_key_readable)))

    public_key_hashed = base64.b64encode(public_key_readable.encode("utf-8")).decode("utf-8")
    address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()
    # import keys

    return key, private_key_readable, public_key_readable, public_key_hashed, address