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
from decimal import Decimal

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
V2_VERSION = 0x02         # so a v2 and a legacy pre-image can never alias even if a gate were missed.
                          # v0x02 (doc/41): operation/openfield are omit-when-empty (FLAGS byte) and the
                          # coinbase carries its mining header in the freed sig/pubkey slots. v0x01 never
                          # activated on mainnet (staged), so bumping the layout here is not live-consensus.
V2_OPENFIELD_MAX = 100000  # legacy/mempool anti-DoS bound for NON-coinbase txs; the post-fork coinbase
                           # openfield is uncapped free-form miner data (doc/41). u32 length prefix.

# doc/41 free-form (operation, openfield) flag bits — shared by the signing and block-hash pre-images.
V2_F_OPERATION = 0x01
V2_F_OPENFIELD = 0x02


def _utf8(x) -> bytes:
    """Canonical UTF-8 bytes of ONE consensus field. ``bytes``/``bytearray`` pass through UNCHANGED — so an
    already-encoded field (a raw openfield, a nonce) is never re-stringified into its Python repr, the trap
    in a bare ``str(x).encode()`` (``str(b'abc')`` -> ``"b'abc'"``). Anything else is stringified then UTF-8
    encoded. One coercion used by every variable v2 field, so the signing and block-hash pre-images can never
    diverge on how a value becomes bytes. Byte-identical to the old per-field idioms whenever the field is a
    ``str`` or ``bytes`` (the only forms the consensus path feeds), so it moves no frozen vector."""
    return bytes(x) if isinstance(x, (bytes, bytearray)) else str(x).encode("utf-8")


def _v2_lp(b: bytes, width: int) -> bytes:
    """A length-prefixed field: `width`-byte little-endian byte-length, then the raw bytes."""
    return len(b).to_bytes(width, "little") + b


def _v2_opof(operation, openfield) -> bytes:
    """doc/41: the trailing free-form (operation, openfield) of a v2 pre-image, OMIT-WHEN-EMPTY. A single
    FLAGS u8 (bit0=operation present, bit1=openfield present) precedes only the present fields, each
    u32-length-prefixed and UNCAPPED. An empty or absent field contributes just its (cleared) flag bit, so
    a tx carrying neither costs one byte, and "absent" can never alias "present-but-empty". Used by BOTH the
    signing pre-image (signature_buffer_v2) and the block-hash pre-image (_v2_tx_bytes) so the two can never
    drift out of lockstep."""
    op_b = b"" if operation is None else _utf8(operation)
    of_b = b"" if openfield is None else _utf8(openfield)
    flags = (V2_F_OPERATION if op_b else 0) | (V2_F_OPENFIELD if of_b else 0)
    out = bytes((flags,))
    if op_b:
        out += _v2_lp(op_b, 4)
    if of_b:
        out += _v2_lp(of_b, 4)
    return out


def signature_buffer_v2(timestamp_cs, address, recipient, amount_units, operation, openfield) -> bytes:
    """Post-hf2 canonical BINARY transaction pre-image (doc/29 §2.A): the exact bytes a post-fork
    signature signs and ``tx_id_v2`` hashes. ``timestamp_cs`` = integer centiseconds (preserves the
    legacy '%.2f' precision), ``amount_units`` = integer atomic units (1 BIS = 1e8). Deterministic,
    little-endian, length-prefixed; signature/public_key excluded. Layout (doc/41):
    MAGIC u8 | VERSION u8 | ts_cs u64 | amount u64 | addr(lp u8) | recip(lp u8) | FLAGS u8 | [op(lp u32)] | [of(lp u32)]
    — operation/openfield are omit-when-empty via FLAGS (see _v2_opof)."""
    return (bytes((V2_MAGIC, V2_VERSION))
            + int(timestamp_cs).to_bytes(8, "little")
            + int(amount_units).to_bytes(8, "little")
            + _v2_lp(_utf8(address), 1) + _v2_lp(_utf8(recipient), 1)
            + _v2_opof(operation, openfield))


def tx_id_v2(timestamp_cs, address, recipient, amount_units, operation, openfield) -> str:
    """Post-hf2 content txid over the BINARY pre-image — blake2b-256, 64 lowercase hex. Same role as the
    legacy ``tx_id`` but over ``signature_buffer_v2`` (integer units, binary layout)."""
    return hashlib.blake2b(
        signature_buffer_v2(timestamp_cs, address, recipient, amount_units, operation, openfield),
        digest_size=32).hexdigest()


def _v2_ts_cs(ts) -> int:
    """A '%.2f'-form timestamp -> integer centiseconds (exact, no float; matches quantize_two precision)."""
    return int((Decimal(str(ts)).quantize(Decimal("0.01")) * 100).to_integral_value())


def _v2_units(amount) -> int:
    """A '%.8f'-form amount -> integer atomic units (exact; matches amounts.to_units, kept inline so the
    frozen consensus module stays dependency-light)."""
    return int((Decimal(str(amount)).quantize(Decimal("0.00000001")) * 100000000).to_integral_value())


def _v2_tx_bytes(tx, is_coinbase=False) -> bytes:
    """Canonical binary encoding of ONE converted 8-field tx tuple for the block hash (doc/29 §2.B):
    (timestamp, address, recipient, amount, signature, public_key, operation, openfield). timestamp ->
    integer centiseconds, amount -> integer atomic units; all variable fields length-prefixed, little-endian.

    hf2 §2.C / doc/41 COINBASE: the last tx (``is_coinbase``) is PoW-authorized, never signed, so its
    signature + public_key wire slots are repurposed as the MINING HEADER and committed here — slot[4] =
    PoW nonce, slot[5] = mining commitment ("vmsr"<root>[+"hf2" vote], doc/19+doc/41). Both are
    u8-length-prefixed and HASHED, so neither the nonce nor the state-root commitment can be ground/forged.
    This frees operation+openfield to be free-form, optional, uncapped miner data (omit-when-empty via
    _v2_opof). (Pre-fork uses the frozen legacy block_hash, so this only affects post-fork.)"""
    head = (_v2_ts_cs(tx[0]).to_bytes(8, "little")
            + _v2_units(tx[3]).to_bytes(8, "little")
            + _v2_lp(_utf8(tx[1]), 1)      # address
            + _v2_lp(_utf8(tx[2]), 1))     # recipient
    if is_coinbase:
        # The mining-header slots use a u8 length prefix, so each must fit in 255 bytes. Validation
        # (digest_tx.Transaction.validate) rejects an over-long coinbase before we get here; guard again so a
        # slot that somehow reaches the hash raises a CLEAN ValueError instead of a bare OverflowError from
        # to_bytes(1). Honest nonce (~16 B) and commitment (~70 B) are far under the bound.
        _nonce_b, _commit_b = _utf8(tx[4]), _utf8(tx[5])
        if len(_nonce_b) > 255 or len(_commit_b) > 255:
            raise ValueError(
                f"coinbase mining-header slot too long for u8 length prefix "
                f"(nonce {len(_nonce_b)} B, commitment {len(_commit_b)} B; max 255)")
        mid = (_v2_lp(_nonce_b, 1)     # PoW nonce (freed signature slot)
               + _v2_lp(_commit_b, 1))  # mining commitment (freed public_key slot)
    else:
        mid = (_v2_lp(_utf8(tx[4]), 2)     # signature
               + _v2_lp(_utf8(tx[5]), 2))  # public_key
    return head + mid + _v2_opof(tx[6], tx[7])           # doc/41: omit-when-empty operation + openfield


def block_hash_v2(transaction_list_converted, previous_hash: str) -> str:
    """Post-hf2 block hash (doc/29 §2.B): blake2b-256 over a canonical BINARY per-tx encoding with native
    integer amount + integer timestamp, replacing sha224(str(8-tuples)+prev). Deterministic and stronger
    than the legacy repr (explicit lengths -> two implementations can't diverge on quoting/escaping).
    previous_hash: a 64-hex post-fork parent is taken as raw bytes; the FIRST post-fork block's parent is
    the last pre-fork 56-hex sha224 hash -> encoded as UTF-8 (one-time boundary case).
    The LAST tx is the coinbase (§2.C): its signature + public_key are omitted from the pre-image."""
    n = len(transaction_list_converted)
    pre = n.to_bytes(4, "little")
    for i, tx in enumerate(transaction_list_converted):
        pre += _v2_tx_bytes(tx, is_coinbase=(i == n - 1))
    prev = str(previous_hash)
    if len(prev) == 64:
        try:
            prev_b = bytes.fromhex(prev)
        except ValueError:
            prev_b = prev.encode("utf-8")
    else:
        prev_b = prev.encode("utf-8")              # pre-fork 56-hex parent at the boundary
    return hashlib.blake2b(pre + prev_b, digest_size=32).hexdigest()


# --- fork-aware dispatchers: legacy below fork_height (byte-identical), v2 at/after (doc/29 §4) ---------
def block_hash_at(height, fork_height, transaction_list_converted, previous_hash: str) -> str:
    """Select the block-hash encoding by the DESTINATION block height. fork_height None or height below it
    -> the frozen legacy sha224 form (pre-fork byte-identity); at/after -> block_hash_v2."""
    if fork_height is not None and height is not None and int(height) >= int(fork_height):
        return block_hash_v2(transaction_list_converted, previous_hash)
    return block_hash(transaction_list_converted, previous_hash)


def tx_id_v2_s(timestamp, address, recipient, amount, operation, openfield) -> str:
    """tx_id_v2 computed from the canonical '%.2f' timestamp + '%.8f' amount STRING forms (the forms a
    stored row reconstructs and a wallet signs), converting them to integer centiseconds / atomic units.
    The post-fork canonical-txid helper for callers that already know they are post-fork."""
    return tx_id_v2(_v2_ts_cs(timestamp), address, recipient, _v2_units(amount), operation, openfield)


def tx_id_at(height, fork_height, timestamp, address, recipient, amount, operation, openfield) -> str:
    """Canonical content txid, fork-aware (doc/29 §4, Stage 2): at/after fork_height -> tx_id_v2 over the
    BINARY pre-image (integer centisecond timestamp + integer atomic-unit amount); below it (or fork_height
    None, e.g. a pre-fork mainnet row) -> the legacy tx_id over the '%.2f'/'%.8f' string buffer. `timestamp`
    is the canonical '%.2f' string and `amount` the canonical '%.8f' string (what the stored row reconstructs
    and what is signed). This is the ONE canonical id of a post-fork tx regardless of which signing scheme
    verified it."""
    if fork_height is not None and height is not None and int(height) >= int(fork_height):
        return tx_id_v2_s(timestamp, address, recipient, amount, operation, openfield)
    return tx_id(timestamp, address, recipient, amount, operation, openfield)
