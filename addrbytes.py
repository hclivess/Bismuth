"""hf2 Stage-4 TRUE-BYTES address/recipient codec (doc/40 — address domain).

A stored tx ``address``/``recipient`` is a base58check (or 56-hex, for RSA / sinks) re-encoding of a
small fixed binary payload, carried today as a msgpack ``str``. Post-fork we store the raw payload
bytes (a genuine msgpack ``bin`` element — TRUE bytes, never hex-in-text) and rebuild the exact string
on read.

THE HARD CONSTRAINT: the field is not always an address. It is truncated to 56 chars at write time and
on the real chain carries non-address sentinels that MUST survive byte-for-byte — ``"genesis"``,
``"Hypernode"``, VM contract addresses, and 56-hex sinks like ``SHIELD_SINK``. base58 is not a safe
universal codec (arbitrary text isn't base58-decodable, and decodable text only round-trips when it is
the *canonical* encoding). So this is a tagged union with a verbatim UTF-8 fallback, and the encoder
PROVES the round-trip before committing to a structured tag; anything that doesn't provably re-encode
to itself lands in the verbatim 0xFF branch. The 0xFF fallback is load-bearing for CORRECTNESS, not a
size knob.

STORAGE only — the consensus pre-image still consumes the reconstructed base58/hex STRING (C5:
storage-raw, dispatch-string). Gated by the caller on ``node.fork_height`` by destination height.
"""

__version__ = "0.0.1"

TAG_HEX      = 0x00   # RSA / 56-hex / 56-hex sink: body = bytes.fromhex(addr)        -> addr = body.hex()
TAG_ECDSA    = 0x01   # secp256k1 Bis1 (short):     body = b58decode(addr)            -> b58encode(body)
TAG_ED25519  = 0x02   # ED25519 Bis1 (long):        body = b58decode(addr)
TAG_VERSIONED= 0x03   # ML-DSA / secp256r1:         body = b58decode(addr)
TAG_MULTISIG = 0x04   # native multisig Bism/mBis:  body = b58decode(addr)
TAG_VERBATIM = 0xFF   # genesis / sinks / contract / non-address / truncated: u8 len || raw UTF-8


def _verbatim(b):
    if len(b) > 255:
        raise ValueError("address/recipient utf-8 (%d) exceeds u8 length bound" % len(b))
    return bytes((TAG_VERBATIM, len(b))) + b


def _tag_for_signer_name(name):
    if name == "SignerECDSA":
        return TAG_ECDSA
    if name == "SignerED25519":
        return TAG_ED25519
    if name == "SignerMultisig":
        return TAG_MULTISIG
    if name in ("SignerSECP256R1", "SignerMLDSA", "SignerMLDSA44", "SignerMLDSA65", "SignerMLDSA87"):
        return TAG_VERSIONED
    return None


def pack_addr(s):
    """WRITE: address/recipient string -> packed TRUE-BYTES blob. Total and self-checking: any value that
    does not provably re-encode to itself takes the verbatim 0xFF branch (lossless, no size win)."""
    s = str(s)
    b = s.encode("utf-8")
    try:
        from polysign.signerfactory import (RE_RSA_ADDRESS, RE_ECDSA_ADDRESS,
                                             RE_MULTISIG_ADDRESS, SignerFactory)
    except Exception:
        return _verbatim(b)

    # 1) RSA / 56-hex / 56-hex sink — the strict ^[abcdef0-9]{56}$ predicate, lowercase-canonical guard.
    if RE_RSA_ADDRESS.match(s):
        try:
            raw = bytes.fromhex(s)
            if raw.hex() == s:                       # canonical lowercase hex
                return bytes((TAG_HEX,)) + raw
        except ValueError:
            pass
        return _verbatim(b)

    # 2) base58 families — classify, then PROVE the b58 round-trip before committing to a fixed tag.
    try:
        import base58
        if RE_MULTISIG_ADDRESS.match(s):
            tag = TAG_MULTISIG
        elif RE_ECDSA_ADDRESS.match(s):
            tag = TAG_ED25519 if len(s) > 50 else TAG_ECDSA
        else:
            tag = _tag_for_signer_name(getattr(SignerFactory._versioned_signer(s), "__name__", ""))
        if tag is not None:
            body = base58.b58decode(s)
            if base58.b58encode(body).decode() == s:  # canonical-encoding guard (preserves leading '1's)
                return bytes((tag,)) + body
    except Exception:
        pass

    # 3) genesis / sinks / contract / any non-address / truncated mid-string -> verbatim.
    return _verbatim(b)


def unpack_addr(value):
    """READ: packed blob -> byte-identical address/recipient string.

    HARD transition guard: a plain ``str`` is a pre-Stage-4 or mid-rollback row -> returned unchanged
    (use_bin_type=True + raw=False make str vs bytes unambiguous on msgpack unpack)."""
    if isinstance(value, str):
        return value
    value = bytes(value)
    tag = value[0]
    body = value[1:]
    if tag == TAG_HEX:
        return body.hex()
    if TAG_ECDSA <= tag <= TAG_MULTISIG:
        import base58
        return base58.b58encode(body).decode()
    if tag == TAG_VERBATIM:
        n = body[0]
        if n > len(body) - 1:        # detect a torn/truncated blob instead of silently returning short
            raise ValueError("verbatim address length %d exceeds buffer %d" % (n, len(body) - 1))
        return body[1:1 + n].decode("utf-8")
    raise ValueError("unknown address tag 0x%02x" % tag)
