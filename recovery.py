from Cryptodome.PublicKey import RSA
import hashlib
import json

def recover(key):
    private_key_readable = key.exportKey().decode("utf-8")
    public_key_readable = key.publickey().exportKey().decode("utf-8")
    address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()

    wallet_dict = {}
    wallet_dict['Private Key'] = private_key_readable
    wallet_dict['Public Key'] = public_key_readable
    wallet_dict['Address'] = address

    with open ("wallet_recovered.der", 'w') as wallet_file:
        json.dump (wallet_dict, wallet_file)

    print ("Wallet recovered to: wallet_recovered.der")
    return (address, "wallet_recovered.der")