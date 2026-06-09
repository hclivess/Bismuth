"""Minimal RSA wallet key generation / loading for Bismuth.

A Bismuth wallet is a 4096-bit RSA key pair; the *address* is the SHA-224 hex
digest of the PEM-exported public key. :func:`generate` creates a fresh pair,
:func:`read` loads one from the JSON ``wallet.der`` file in the working
directory. These derivations are consensus-relevant (the address must match what
signatures are checked against), so the byte forms here are kept exactly as-is.
"""
import base64, hashlib, json
from Cryptodome.PublicKey import RSA


def generate() -> tuple:
    """Generate a fresh 4096-bit RSA wallet.

    Returns:
        tuple: ``(private_key_readable, public_key_readable, address)`` where the
        readable keys are PEM strings and ``address`` is the SHA-224 hex digest
        of the public key.
    """
    # generate key pair and an address
    key = RSA.generate(4096)

    private_key_readable = key.exportKey().decode("utf-8")
    public_key_readable = key.publickey().exportKey().decode("utf-8")
    address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()  # hashed public key
    return private_key_readable, public_key_readable, address


def read() -> tuple:
    """Load the wallet from ``wallet.der`` in the current directory.

    Validates the public-key length (271 or 799 chars) and re-derives the
    base64-encoded public key and the SHA-224 address.

    Returns:
        tuple: ``(key, private_key_readable, public_key_readable,
        public_key_b64encoded, address)``. ``key`` is the raw private-key PEM
        string (kept for backward-compatible call sites).
    """
    # import keys
    with open ("wallet.der", 'r') as wallet_file:
            wallet_dict = json.load (wallet_file)
    private_key_readable = wallet_dict['Private Key']
    public_key_readable = wallet_dict['Public Key']
    key = private_key_readable

    if (len(public_key_readable)) != 271 and (len(public_key_readable)) != 799:
        raise ValueError("Invalid public key length: {}".format(len(public_key_readable)))

    public_key_b64encoded = base64.b64encode(public_key_readable.encode("utf-8")).decode("utf-8")
    address = hashlib.sha224(public_key_readable.encode("utf-8")).hexdigest()
    # import keys

    return key, private_key_readable, public_key_readable, public_key_b64encoded, address
