"""
Frozen consensus serialization for Bismuth.

The byte forms used for transaction SIGNING and BLOCK HASHING are consensus-critical: change them and
signatures / block hashes change, and the network forks. Historically these byte forms were spelled
out inline in several places (digest.py, essentials.sign_rsa, mempool.merge). They are centralised
here so the rest of the node — and any future storage / API rework (see doc/16) — can evolve without
ever touching the consensus bytes.

These functions reproduce the legacy forms EXACTLY and are locked by characterization vectors in
tests/test_characterization.py. DO NOT change their output.
"""
import hashlib

__version__ = "0.0.1"

# Canonical per-transaction field order (8 fields), as stored and as fed into the block hash.
TX_FIELDS = ("timestamp", "address", "recipient", "amount",
             "signature", "public_key", "operation", "openfield")


def signature_buffer(timestamp, address, recipient, amount, operation, openfield) -> bytes:
    """Exact bytes a Bismuth transaction signature is computed / verified over.

    Legacy form: the ``repr`` of the 6-field tuple, UTF-8 encoded. Callers pass the canonical string
    forms (timestamp ``'%.2f'``, amount ``'%.8f'``); this function does not reformat them.
    """
    return str((timestamp, address, recipient, amount, operation, openfield)).encode("utf-8")


def tx_id(timestamp, address, recipient, amount, operation, openfield) -> str:
    """Post-hf2 content-addressed transaction id (nado model, doc/16): BLAKE2b-256 (32 bytes) over the
    SAME frozen pre-image the signature is computed on (``signature_buffer``), hex-encoded -> 64 lowercase
    hex chars. Signature and public_key are excluded by construction, so the id is a stable, URL-safe
    commitment to the tx content (not a slice of the signature). blake2b is the hf2 hash choice (not
    keccak). Reuses ``signature_buffer`` as the single canonical pre-image so the txid can never drift
    away from what is signed."""
    return hashlib.blake2b(
        signature_buffer(timestamp, address, recipient, amount, operation, openfield),
        digest_size=32).hexdigest()


def signed_message(txid_hex: str) -> bytes:
    """Post-hf2 the signature signs the txid itself (Ethereum-shape: sign a 32-byte content hash). This
    returns the 32 raw bytes of the hex txid that a single-sig secp256k1 signer signs / is recovered over."""
    return bytes.fromhex(txid_hex)


def block_hash(transaction_list_converted, previous_hash: str) -> str:
    """Exact Bismuth block hash: sha224 over ``repr`` of the list of 8-field transaction tuples,
    concatenated with the previous block's hash."""
    return hashlib.sha224(
        (str(transaction_list_converted) + previous_hash).encode("utf-8")
    ).hexdigest()
