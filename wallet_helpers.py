"""
Wallet & key management for Bismuth.

RSA keypair generation / load / save / unlock and transaction signing (``sign_rsa``). Lifted out of
``essentials`` (and re-exported there for compatibility) so the key-handling cluster lives in one
focused module. Consensus note: ``sign_rsa`` routes through the frozen
``bismuth_serialize.signature_buffer`` — the signing-buffer bytes are unchanged.
"""
import base64
import getpass
import hashlib
import json
import os
from typing import Union

from Cryptodome.PublicKey import RSA
from simplecrypt import decrypt
from polysign.signer import SignerType
from polysign.signerfactory import SignerFactory

import bismuth_serialize


def sign_rsa(timestamp, address, recipient, amount, operation, openfield, key, public_key_b64encoded) -> Union[bool, tuple]:
    # TODO: move, make use of polysign module
    if not key:
        raise BaseException("The wallet is locked, you need to provide a decrypted key")
    try:
        transaction = (str(timestamp), str(address), str(recipient), '%.8f' % float(amount), str(operation), str(openfield))
        # this is signed, float kept for compatibility
        buffer = bismuth_serialize.signature_buffer(*transaction)
        signer = SignerFactory.from_private_key(key.exportKey().decode("utf-8"), SignerType.RSA)
        signature_enc = signer.sign_buffer_for_bis(buffer)
        # Extra: recheck - Raises if Error
        SignerFactory.verify_bis_signature(signature_enc, public_key_b64encoded, buffer, address)
        full_tx = str(timestamp), str(address), str(recipient), '%.8f' % float(amount), \
                  str(signature_enc.decode("utf-8")), str(public_key_b64encoded.decode("utf-8")), \
                  str(operation), str(openfield)
        return full_tx
    except:
        return False


def keys_check(app_log, keyfile_name: str) -> None:
    # TODO: move, make use of polysign module
    # key maintenance
    if os.path.isfile("privkey.der") is True:
        app_log.warning("privkey.der found")
    elif os.path.isfile("privkey_encrypted.der") is True:
        app_log.warning("privkey_encrypted.der found")
        os.rename("privkey_encrypted.der", "privkey.der")

    elif os.path.isfile(keyfile_name) is True:
        app_log.warning("{} found".format(keyfile_name))
    else:
        # generate key pair and an address
        key = RSA.generate(4096)
        # public_key = key.publickey()

        private_key_readable = key.exportKey().decode("utf-8")
        public_key_readable = key.publickey().exportKey().decode("utf-8")
        address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()  # hashed public key
        # generate key pair and an address

        app_log.info("Your address: {}".format(address))
        app_log.info("Your public key: {}".format(public_key_readable))

        # export to single file
        keys_save(private_key_readable, public_key_readable, address, keyfile_name)
        # export to single file


def keys_save(private_key_readable: str, public_key_readable: str, address: str, file) -> None:
    wallet_dict = dict()
    wallet_dict['Private Key'] = private_key_readable
    wallet_dict['Public Key'] = public_key_readable
    wallet_dict['Address'] = address
    if not isinstance(file, str):
        file = file.name
    with open(file, 'w') as keyfile:
        json.dump(wallet_dict, keyfile)


def keys_load(privkey_filename: str = "privkey.der", pubkey_filename: str = "pubkey.der"):
    keyfile = "wallet.der"
    if os.path.exists("wallet.der"):
        print("Using modern wallet method")
        return keys_load_new("wallet.der")

    else:
        # print("loaded",privkey, pubkey)
        # import keys
        try:  # unencrypted
            with open(privkey_filename) as fp:
                key = RSA.importKey(fp.read())
            private_key_readable = key.exportKey().decode("utf-8")
            # public_key = key.publickey()
            encrypted = False
            unlocked = True
        except:  # encrypted
            encrypted = True
            unlocked = False
            key = None
            with open(privkey_filename) as fp:
                private_key_readable = fp.read()

        # public_key_readable = str(key.publickey().exportKey())
        with open(pubkey_filename.encode('utf-8')) as fp:
            public_key_readable = fp.read()

        if len(public_key_readable) not in (271, 799):
            raise ValueError("Invalid public key length: {}".format(len(public_key_readable)))

        public_key_b64encoded = base64.b64encode(public_key_readable.encode('utf-8'))
        address = hashlib.sha224(public_key_readable.encode('utf-8')).hexdigest()

        print("Upgrading wallet")
        keys_save(private_key_readable, public_key_readable, address, keyfile)

        return key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_b64encoded, address, keyfile


def keys_unlock(private_key_encrypted: str) -> tuple:
    password = getpass.getpass()
    encrypted_privkey = private_key_encrypted
    decrypted_privkey = decrypt(password, base64.b64decode(encrypted_privkey))
    key = RSA.importKey(decrypted_privkey)  # be able to sign
    private_key_readable = key.exportKey().decode("utf-8")
    return key, private_key_readable


def keys_load_new(keyfile="wallet.der"):
    # import keys

    with open(keyfile, 'r') as keyfile:
        wallet_dict = json.load(keyfile)

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
    if len(public_key_readable) not in (271, 799):
        raise ValueError("Invalid public key length: {}".format(len(public_key_readable)))

    public_key_b64encoded = base64.b64encode(public_key_readable.encode('utf-8'))

    return key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_b64encoded, address, keyfile
