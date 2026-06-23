#!/usr/bin/env python3
"""gen_test_wallet.py — make a THROWAWAY RSA wallet in the node's wallet.der JSON shape (Private/Public Key
PEM + sha224(PEM) address) so the signer roundtrip can sign in JS and verify with the node's verifier.
NOT for real funds; regenerated each run."""
import hashlib
import json
import os

from Cryptodome.PublicKey import RSA

here = os.path.dirname(os.path.abspath(__file__))
key = RSA.generate(2048)
priv = key.exportKey().decode("utf-8")
pub = key.publickey().exportKey().decode("utf-8")
address = hashlib.sha224(pub.encode("utf-8")).hexdigest()  # matches SignerRsa.public_key_to_address

with open(os.path.join(here, "test_wallet.json"), "w") as f:
    json.dump({"Private Key": priv, "Public Key": pub, "Address": address}, f)
print("wrote throwaway test_wallet.json  address=%s" % address)
