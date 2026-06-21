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


# === hf2 binary/integer serialization (doc/29) — POST-FORK variants, gated by the CALLER on fork_height ===
# Added BESIDE the frozen legacy functions above; the legacy forms are NEVER mutated (characterization-
# locked, tests/test_characterization.py). These are inert until a consensus site routes through a
# fork-aware dispatcher (staged rollout, doc/29 §4). A post-fork tx pre-image is a canonical little-endian
# BINARY encoding over the SAME six content fields, with NATIVE INTEGER amount (atomic units) and an
# integer centisecond timestamp — no '%.8f'/'%.2f' string reconstruction. signature and public_key are
# excluded by construction (like the legacy buffer). The binary form is only ever BUILT from validated
# fields for signing/hashing — it is not a wire format (the wire stays the 8-field tuple), so no decoder
# is needed on the consensus path.
V2_MAGIC = 0xB2            # 0xB2 can't begin a legacy pre-image (legacy starts with ASCII '(' = 0x28),
V2_VERSION = 0x01         # so a v2 and a legacy pre-image can never alias even if a gate were missed.
V2_OPENFIELD_MAX = 100000  # consensus cap (matches digest_tx truncation), u32 length prefix


def _v2_lp(b: bytes, width: int) -> bytes:
    """A length-prefixed field: `width`-byte little-endian byte-length, then the raw bytes."""
    return len(b).to_bytes(width, "little") + b


def signature_buffer_v2(timestamp_cs, address, recipient, amount_units, operation, openfield) -> bytes:
    """Post-hf2 canonical BINARY transaction pre-image (doc/29 §2.A): the exact bytes a post-fork
    signature signs and ``tx_id_v2`` hashes. ``timestamp_cs`` = integer centiseconds (preserves the
    legacy '%.2f' precision), ``amount_units`` = integer atomic units (1 BIS = 1e8). Deterministic,
    little-endian, length-prefixed; signature/public_key excluded. Layout:
    MAGIC u8 | VERSION u8 | ts_cs u64 | amount u64 | addr(lp u8) | recip(lp u8) | op(lp u8) | openfield(lp u32)."""
    addr_b = str(address).encode("utf-8")
    recip_b = str(recipient).encode("utf-8")
    op_b = str(operation).encode("utf-8")
    of_b = openfield.encode("utf-8") if isinstance(openfield, str) else bytes(openfield)
    return (bytes((V2_MAGIC, V2_VERSION))
            + int(timestamp_cs).to_bytes(8, "little")
            + int(amount_units).to_bytes(8, "little")
            + _v2_lp(addr_b, 1) + _v2_lp(recip_b, 1) + _v2_lp(op_b, 1) + _v2_lp(of_b, 4))


def tx_id_v2(timestamp_cs, address, recipient, amount_units, operation, openfield) -> str:
    """Post-hf2 content txid over the BINARY pre-image — blake2b-256, 64 lowercase hex. Same role as the
    legacy ``tx_id`` but over ``signature_buffer_v2`` (integer units, binary layout)."""
    return hashlib.blake2b(
        signature_buffer_v2(timestamp_cs, address, recipient, amount_units, operation, openfield),
        digest_size=32).hexdigest()
