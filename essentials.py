"""
Common helpers for Bismuth - Optimized Version
"""
import base64
import getpass
import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from functools import lru_cache

import requests
# from Crypto import Random
from Cryptodome.PublicKey import RSA

from quantizer import quantize_two, quantize_eight, quantize_ten
from decimal import Decimal
from simplecrypt import encrypt, decrypt
from typing import Union
from polysign.signer import SignerType
from polysign.signerfactory import SignerFactory

import db_helpers

__version__ = "0.0.7"

"""
0.0.7 : decrease checkpoint limit to 30 blocks at 1450000 (meaning max 59 blocks rollback)
"""

# Constants to avoid recreating Decimal objects
DECIMAL_ZERO = Decimal(0)
DECIMAL_ONE = Decimal(1)
DECIMAL_TEN = Decimal(10)
DECIMAL_HUNDRED_THOUSAND = Decimal(100000)
BASE_FEE = Decimal("0.01")

# Regex cache for replace_regex function
_regex_cache = {}

"""
For temp. code compatibility, dup code moved to polysign module
"""


@lru_cache(maxsize=1024)
def address_validate(address: str) -> bool:
    return SignerFactory.address_is_valid(address)


@lru_cache(maxsize=1024)
def address_is_rsa(address: str) -> bool:
    return SignerFactory.address_is_rsa(address)


"""
End compatibility
"""


def format_raw_tx(raw: list) -> dict:
    # Pre-size dictionary with all keys for better performance
    transaction = {
        'block_height': raw[0],
        'timestamp': raw[1],
        'address': raw[2],
        'recipient': raw[3],
        'amount': raw[4],
        'signature': raw[5],
        'txid': raw[5][:56],
        'block_hash': raw[7],
        'fee': raw[8],
        'reward': raw[9],
        'operation': raw[10],
        'openfield': raw[11]
    }

    # Only try base64 decode if it looks like base64
    if isinstance(raw[6], str) and len(raw[6]) > 0:
        try:
            transaction['pubkey'] = base64.b64decode(raw[6]).decode('utf-8')
        except:
            transaction['pubkey'] = raw[6]  # support new pubkey schemes
    else:
        transaction['pubkey'] = raw[6]

    return transaction


def percentage(percent, whole):
    return Decimal(percent) * Decimal(whole) / 100


def replace_regex(string: str, replace: str) -> str:
    # Cache compiled regex patterns for performance
    if replace not in _regex_cache:
        _regex_cache[replace] = re.compile(r'^{}'.format(re.escape(replace)))
    return _regex_cache[replace].sub("", string)


def download_file(url: str, filename: str) -> None:
    """Download a file from URL to filename

    :param url: URL to download file from
    :param filename: Filename to save downloaded data as

    returns `filename`
    """
    try:
        r = requests.get(url, stream=True)
        total_size = int(r.headers.get('content-length', 0)) / 1024

        with open(filename, 'wb') as fp:
            downloaded = 0
            last_percent = 0

            # Use larger chunks for better I/O performance
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    fp.write(chunk)
                    downloaded += len(chunk) / 1024

                    if total_size > 0:
                        percent = int(100 * (downloaded / total_size))
                        if percent > last_percent and percent % 10 == 0:
                            print(f"Downloaded {percent} %")
                            last_percent = percent

            print("Downloaded 100 %")
    except:
        raise


def most_common(lst: list):
    """Used by consensus - optimized with Counter"""
    if not lst:
        return None
    return Counter(lst).most_common(1)[0][0]


def most_common_dict(a_dict: dict):
    """Returns the most common value from a dict. Used by consensus"""
    return max(a_dict.values())


def percentage_in(individual, whole):
    return (float(list(whole).count(individual) / float(len(whole)))) * 100


def round_down(number, order):
    return int(math.floor(number / order)) * order


def checkpoint_set(node):
    limit = 30
    if node.last_block < 1450000:
        limit = 1000
    checkpoint = round_down(node.last_block, limit) - limit
    if checkpoint != node.checkpoint:
        node.checkpoint = checkpoint
        node.logger.app_log.warning(f"Checkpoint set to {node.checkpoint}")


def ledger_balance3(address, cache, db_handler):
    # Many heavy blocks are pool payouts, same address.
    # Cache pre_balance instead of recalc for every tx
    if address in cache:
        return cache[address]

    # Optimized: Single query instead of two separate queries
    db_handler.execute_param(db_handler.c,
        """SELECT 
           SUM(CASE WHEN recipient = ? THEN amount + reward ELSE 0 END) as credit,
           SUM(CASE WHEN address = ? THEN amount + fee ELSE 0 END) as debit
           FROM transactions 
           WHERE recipient = ? OR address = ?""",
        (address, address, address, address))

    result = db_handler.c.fetchone()
    credit = Decimal(result[0] or 0)
    debit = Decimal(result[1] or 0)

    cache[address] = quantize_eight(credit - debit)
    return cache[address]


def ledger_balance3_original(address, cache, db_handler):
    """Keep original implementation as fallback if needed"""
    if address in cache:
        return cache[address]
    credit_ledger = Decimal(0)

    db_handler.execute_param(db_handler.c, "SELECT amount, reward FROM transactions WHERE recipient = ?;", (address,))
    entries = db_handler.c.fetchall()

    for entry in entries:
        credit_ledger += quantize_eight(entry[0]) + quantize_eight(entry[1])

    debit_ledger = Decimal(0)
    db_handler.execute_param(db_handler.c, "SELECT amount, fee FROM transactions WHERE address = ?;", (address,))
    entries = db_handler.c.fetchall()

    for entry in entries:
        debit_ledger += quantize_eight(entry[0]) + quantize_eight(entry[1])

    cache[address] = quantize_eight(credit_ledger - debit_ledger)
    return cache[address]


def sign_rsa(timestamp, address, recipient, amount, operation, openfield, key, public_key_b64encoded) -> Union[bool, tuple]:
    # TODO: move, make use of polysign module
    if not key:
        raise BaseException("The wallet is locked, you need to provide a decrypted key")
    try:
        transaction = (str(timestamp), str(address), str(recipient), '%.8f' % float(amount), str(operation), str(openfield))
        # this is signed, float kept for compatibility
        buffer = str(transaction).encode("utf-8")
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


def fee_calculate(openfield: str, operation: str = '', block: int = 0) -> Decimal:
    # block var will be removed after HF
    # Optimized: use pre-defined constants and check operation first
    fee = BASE_FEE + (Decimal(len(openfield)) / DECIMAL_HUNDRED_THOUSAND)

    # Check operation first (faster than string.startswith)
    if operation == "token:issue":
        fee += DECIMAL_TEN
    elif openfield.startswith("alias="):  # Only check if needed
        fee += DECIMAL_ONE
    # if operation == "alias:register": #add in the future, careful about forking
    #    fee = Decimal(fee) + Decimal("1")

    return quantize_eight(fee)


def execute_param_c(cursor, query, param, app_log):
    """Secure execute w/ param for slow nodes (aborts on UnicodeEncodeError, retries otherwise)."""
    db_helpers.retry_db(lambda: cursor.execute(query, param), abort_on=(UnicodeEncodeError,),
                        delay=0.1, log=app_log, describe=str(query)[:100])
    return cursor


def is_sequence(arg) -> bool:
    # TODO: hard to read compound condition.
    return not hasattr(arg, "strip") and hasattr(arg, "__getitem__") or hasattr(arg, "__iter__")