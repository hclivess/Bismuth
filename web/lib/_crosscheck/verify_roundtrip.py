#!/usr/bin/env python3
"""verify_roundtrip.py — load the JS-signed tx and verify it with the NODE's real signature verifier
(polysign.SignerFactory.verify_tx_signature, the same call mempool.merge makes). Proves the JS signer in
bismuth-tx.js is byte-compatible with the consensus signature scheme. No network, no ledger."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for p in (ROOT, os.path.join(ROOT, "polysign")):
    if p not in sys.path:
        sys.path.insert(0, p)

from polysign.signerfactory import SignerFactory

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "signed_tx.json")) as f:
    tx = json.load(f)["tx"]

# wire order: [timestamp, address, recipient, amount, signature, public_key, operation, openfield]
ts, address, recipient, amount, signature, public_key, operation, openfield = tx

# CLASSIC (pre-hf2 / RSA) path: post_fork=False -> verifies the legacy signature_buffer with the explicit
# public key. This is exactly what mempool.merge runs for an RSA sender.
SignerFactory.verify_tx_signature(False, ts, address, recipient, amount, operation, openfield,
                                  signature, public_key)
print("NODE VERIFIER ACCEPTED the JS-signed tx (RSA-PKCS1-v1_5 / SHA-1 over the canonical buffer)")
print("  address  :", address)
print("  amount   :", amount)
print("  operation:", operation)
