"""hf2 Stage-4 TRUE-BYTES signature codec (doc/40 — signatures domain).

The transaction ``signature`` field is carried on the wire and in the legacy SQLite ledger as a
base64 (RSA/ECDSA/ED25519/secp256r1/ML-DSA/multisig) or lowercase-hex (the hf2 recoverable
secp256k1 form) TEXT string. Inside a *binary* store (the LMDB ``block_store`` msgpack value) that
base64 inflation (~1.33x) buys nothing — it is an A-hex-class regression in disguise. Post-fork this
module stores the *decoded raw signature bytes* with a 1-byte scheme tag + a u16 length, and rebuilds
the byte-identical wire string on read.

This is a STORAGE codec only. It does NOT touch any frozen consensus pre-image: the signature is
excluded from ``signature_buffer`` / ``tx_id`` / ``block_hash`` and their v2 siblings by construction;
the only pre-image it feeds is ``_v2_tx_bytes`` (the block hash), and there it is consumed AS STORED
with a length prefix — so as long as the rebuilt wire string is byte-identical (it is; see the
round-trip proof / tests), the block hash is unperturbed.

Reversibility holds because every decode is a bijection on the set of *canonical* wire strings:
- base64 (Bismuth always emits canonical padded base64): ``b64encode(b64decode(x)).decode() == x``;
- hex (tag 0x40): ``bytes.fromhex(x).hex() == x`` for lowercase even-length x.
Any non-canonical legacy string is detected by the write-time self-check and stored verbatim under
the opaque tag (0x00), guaranteeing losslessness at zero size benefit for that one row.

RSA is SINGLE base64 (one ``b64decode`` on verify, one ``b64encode`` on sign — polysign/signer_rsa.py),
verified against real mainnet rows across every era; the raw width is variable (128 B for legacy
1024-bit keys, 512 B for 4096-bit keys), carried by the dynamic u16 length prefix.

Gated by the CALLER on ``node.fork_height`` by destination block height (None-means-legacy): pre-fork
rows keep the legacy ``str`` form untouched, so pre-fork byte-identity is structural.
"""
import base64

__version__ = "0.0.1"

# --- scheme tags (doc/40 §1.2). The tag is stored, so to_wire() rebuilds from the tag ALONE. ---
TAG_OPAQUE      = 0x00   # non-canonical / unknown legacy text -> stored verbatim as UTF-8 (fallback)
TAG_RSA         = 0x01   # RSA PKCS#1 v1.5 — SINGLE base64
TAG_ECDSA       = 0x02   # secp256k1 legacy DER — base64
TAG_ED25519     = 0x03   # ED25519 — base64
TAG_SECP256R1   = 0x04   # secp256r1 / P-256 DER — base64
TAG_MLDSA44     = 0x05   # ML-DSA-44 — base64
TAG_MLDSA65     = 0x06   # ML-DSA-65 — base64
TAG_MLDSA87     = 0x07   # ML-DSA-87 — base64
TAG_MULTISIG    = 0x08   # native M-of-N multisig blob — base64 (opaque blob, preserved verbatim)
TAG_BTC         = 0x09   # BTC interop — base64
TAG_CRW         = 0x0A   # CRW interop — base64
TAG_RECOVERABLE = 0x40   # hf2 secp256k1 recoverable r||s||v (65 B) — lowercase HEX, not base64

_MAX_RAW = 0xFFFF        # u16 length prefix ceiling (largest real sig is ML-DSA-87 ~4627 B)


def _decode(tag, wire):
    """Wire string -> raw bytes, per tag class."""
    if tag == TAG_OPAQUE:
        return wire.encode("utf-8")
    if tag == TAG_RECOVERABLE:
        return bytes.fromhex(wire)
    return base64.b64decode(wire)


def _encode(tag, raw):
    """Raw bytes -> byte-identical wire string, per tag class."""
    if tag == TAG_OPAQUE:
        return raw.decode("utf-8")
    if tag == TAG_RECOVERABLE:
        return raw.hex()
    return base64.b64encode(raw).decode("ascii")


def pack_signature(tag, raw):
    """Low-level: ``u8(tag) || u16le(len) || raw`` (TRUE bytes — no base64, no hex)."""
    if len(raw) > _MAX_RAW:
        raise ValueError("signature raw length %d exceeds u16 prefix" % len(raw))
    return bytes((tag,)) + len(raw).to_bytes(2, "little") + raw


def unpack_signature(blob):
    """Low-level inverse: packed blob -> ``(tag, raw)``."""
    blob = bytes(blob)
    if len(blob) < 3:
        raise ValueError("signature blob too short")
    tag = blob[0]
    n = int.from_bytes(blob[1:3], "little")
    raw = blob[3:3 + n]
    if len(raw) != n:
        raise ValueError("signature blob truncated: want %d raw bytes, have %d" % (n, len(raw)))
    return tag, raw


def _looks_recoverable_hex(s):
    """True iff s is the 130-char lowercase-hex (65-byte r||s||v) recoverable secp256k1 wire form."""
    if len(s) != 130:
        return False
    try:
        return bytes.fromhex(s).hex() == s
    except ValueError:
        return False


def tag_for_address(address, signature_str=""):
    """Derive the scheme tag from the address (and, for single-sig secp256k1, the signature wire shape
    to disambiguate the hf2 recoverable hex form from the legacy DER base64 form).

    Best-effort: any address this cannot confidently classify returns TAG_OPAQUE, and pack_from_wire's
    self-check then guarantees losslessness regardless (only the size win is lost for that row). Uses the
    authoritative regexes/dispatch from polysign.signerfactory, imported lazily so this codec stays light.
    """
    a = str(address)
    try:
        from polysign.signerfactory import (RE_RSA_ADDRESS, RE_ECDSA_ADDRESS,
                                             RE_MULTISIG_ADDRESS, SignerFactory)
    except Exception:
        return TAG_OPAQUE
    if RE_RSA_ADDRESS.match(a):
        return TAG_RSA
    if RE_MULTISIG_ADDRESS.match(a):
        return TAG_MULTISIG
    if RE_ECDSA_ADDRESS.match(a):
        if len(a) > 50:
            return TAG_ED25519
        # single-sig secp256k1: hf2 recoverable hex (0x40) vs legacy DER base64 (0x02)
        return TAG_RECOVERABLE if _looks_recoverable_hex(signature_str) else TAG_ECDSA
    # versioned post-quantum / NIST families (ML-DSA, secp256r1) — map signer class -> tag
    try:
        sc = SignerFactory._versioned_signer(a)
        name = getattr(sc, "__name__", "")
        return {"SignerSECP256R1": TAG_SECP256R1, "SignerMLDSA44": TAG_MLDSA44,
                "SignerMLDSA": TAG_MLDSA65, "SignerMLDSA65": TAG_MLDSA65,
                "SignerMLDSA87": TAG_MLDSA87}.get(name, TAG_OPAQUE)
    except Exception:
        return TAG_OPAQUE


def pack_from_wire(signature_str, address):
    """WRITE: wire signature string + address -> packed TRUE-BYTES blob.

    Derives the tag, strips the wire envelope to raw bytes, and verifies the round-trip
    (``_encode(_decode(s)) == s``) before committing to the tag. On ANY failure (non-canonical base64/hex,
    unknown scheme) it falls back to TAG_OPAQUE storing the original string verbatim — lossless, no size win.
    """
    s = signature_str if isinstance(signature_str, str) else str(signature_str)
    tag = tag_for_address(address, s)
    if tag != TAG_OPAQUE:
        try:
            raw = _decode(tag, s)
            if _encode(tag, raw) != s:      # canonicality self-check (round-trip guard)
                raise ValueError("non-canonical wire signature for tag 0x%02x" % tag)
        except Exception:
            tag = TAG_OPAQUE
    if tag == TAG_OPAQUE:
        raw = s.encode("utf-8")
    return pack_signature(tag, raw)


def to_wire(blob):
    """READ: packed blob -> byte-identical wire signature string (branches on the stored tag alone)."""
    tag, raw = unpack_signature(blob)
    return _encode(tag, raw)
