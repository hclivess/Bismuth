"""Round-trip / byte-identity tests for the hf2 Stage-4 TRUE-BYTES storage codecs (doc/40).

These prove the lossless-reconstruction invariant for the two foundational, pure codecs:
  - sigbytes  : transaction signature field (per signer scheme)
  - addrbytes : address / recipient field (tagged union + verbatim fallback)

The codecs are storage-only and not yet wired into block_store's consensus path; these tests exercise
them in isolation (no node, no ledger), which is exactly where the round-trip guarantee must hold.

Run with: python3 -m pytest tests/test_storage_codecs.py -v
"""
import base64

import pytest

import sigbytes
import addrbytes


# --------------------------------------------------------------------------- signatures: pure core
def _b64(n, seed=b"\x01"):
    # deterministic raw bytes of length n -> canonical base64 wire string
    raw = (seed * ((n // len(seed)) + 1))[:n]
    return base64.b64encode(raw).decode("ascii"), raw


@pytest.mark.parametrize("tag,n", [
    (sigbytes.TAG_RSA, 128),        # legacy 1024-bit RSA key
    (sigbytes.TAG_RSA, 512),        # 4096-bit RSA key
    (sigbytes.TAG_ECDSA, 71),       # secp256k1 legacy DER
    (sigbytes.TAG_ED25519, 64),     # ed25519
    (sigbytes.TAG_SECP256R1, 71),   # P-256 DER
    (sigbytes.TAG_MLDSA44, 1312),
    (sigbytes.TAG_MLDSA65, 3309),
    (sigbytes.TAG_MLDSA87, 4627),
    (sigbytes.TAG_MULTISIG, 146),
    (sigbytes.TAG_BTC, 71),
    (sigbytes.TAG_CRW, 71),
])
def test_sig_base64_roundtrip(tag, n):
    wire, raw = _b64(n)
    blob = sigbytes.pack_signature(tag, raw)
    assert sigbytes.to_wire(blob) == wire
    t2, r2 = sigbytes.unpack_signature(blob)
    assert (t2, r2) == (tag, raw)
    # blob is TRUE bytes: 3-byte header + raw, NOT base64 length
    assert len(blob) == 3 + n
    assert len(blob) < len(wire) or n < 4   # base64 inflation is removed


def test_sig_recoverable_hex_roundtrip():
    raw = bytes(range(65))                    # 65-byte r||s||v
    wire = raw.hex()                          # 130 lowercase hex chars
    blob = sigbytes.pack_signature(sigbytes.TAG_RECOVERABLE, raw)
    assert sigbytes.to_wire(blob) == wire
    assert len(blob) == 3 + 65               # 68 vs 130 hex chars (~48% smaller)


def test_sig_empty():
    blob = sigbytes.pack_signature(sigbytes.TAG_ECDSA, b"")
    assert blob == bytes((sigbytes.TAG_ECDSA, 0, 0))
    assert sigbytes.to_wire(blob) == ""


def test_sig_opaque_roundtrip():
    s = "this::is::not::valid::base64::!@#"
    blob = sigbytes.pack_signature(sigbytes.TAG_OPAQUE, s.encode("utf-8"))
    assert sigbytes.to_wire(blob) == s


def test_sig_unpack_truncated_raises():
    with pytest.raises(ValueError):
        sigbytes.unpack_signature(bytes((sigbytes.TAG_RSA, 0x10, 0x00)) + b"\x00\x00")  # claims 16, has 2


def test_sig_oversize_raises():
    with pytest.raises(ValueError):
        sigbytes.pack_signature(sigbytes.TAG_MLDSA87, b"\x00" * (0xFFFF + 1))


# --------------------------------------------------------------- signatures: address-driven pack_from_wire
def test_sig_pack_from_wire_rsa():
    addr = "a" * 56                           # RSA / 56-hex address
    wire, _ = _b64(512)
    blob = sigbytes.pack_from_wire(wire, addr)
    assert blob[0] == sigbytes.TAG_RSA
    assert sigbytes.to_wire(blob) == wire


def test_sig_pack_from_wire_ed25519_vs_ecdsa():
    short = "Bis1" + "a" * 30                  # len 34 <= 50 -> secp256k1
    long = "Bis1" + "a" * 52                   # len 56  > 50 -> ed25519
    wire, _ = _b64(64)
    assert sigbytes.pack_from_wire(wire, short)[0] == sigbytes.TAG_ECDSA
    assert sigbytes.pack_from_wire(wire, long)[0] == sigbytes.TAG_ED25519


def test_sig_pack_from_wire_recoverable_disambiguation():
    short = "Bis1" + "a" * 30                  # secp256k1 single-sig address
    hexwire = bytes(range(65)).hex()           # 130-char lowercase hex -> recoverable
    b64wire, _ = _b64(71)                      # legacy DER base64
    assert sigbytes.pack_from_wire(hexwire, short)[0] == sigbytes.TAG_RECOVERABLE
    assert sigbytes.pack_from_wire(b64wire, short)[0] == sigbytes.TAG_ECDSA
    # both round-trip
    assert sigbytes.to_wire(sigbytes.pack_from_wire(hexwire, short)) == hexwire
    assert sigbytes.to_wire(sigbytes.pack_from_wire(b64wire, short)) == b64wire


def test_sig_pack_from_wire_noncanonical_falls_back_to_opaque():
    addr = "a" * 56
    bad = "not valid base64 @@@"              # decodes-with-garbage / fails self-check
    blob = sigbytes.pack_from_wire(bad, addr)
    assert blob[0] == sigbytes.TAG_OPAQUE
    assert sigbytes.to_wire(blob) == bad      # still lossless


# --------------------------------------------------------------------------- addresses
def test_addr_rsa_hex_roundtrip():
    s = "ab" * 28                              # 56 lowercase hex
    blob = addrbytes.pack_addr(s)
    assert blob[0] == addrbytes.TAG_HEX
    assert addrbytes.unpack_addr(blob) == s
    assert len(blob) == 1 + 28                 # 29 vs 56 chars (~47% smaller)


def _b58_addr(prefix, lo, hi):
    """A base58 address: prefix + tail, with total length in [lo, hi] (to satisfy the signerfactory regex)."""
    import base58
    n = max(1, (lo - len(prefix)) // 2)
    for _ in range(200):
        s = prefix + base58.b58encode(bytes([7]) * n).decode()
        if lo <= len(s) <= hi:
            return s
        n += 1 if len(s) < lo else -1
    raise AssertionError("could not build %s addr in [%d,%d]" % (prefix, lo, hi))


def test_addr_base58_families_roundtrip():
    ecdsa = _b58_addr("Bis1", 32, 50)         # RE_ECDSA, len<=50 -> secp256k1
    ed = _b58_addr("Bis1", 51, 56)            # RE_ECDSA, len>50  -> ed25519
    multi = _b58_addr("Bism", 32, 56)         # RE_MULTISIG
    for s, tag in [(ecdsa, addrbytes.TAG_ECDSA), (ed, addrbytes.TAG_ED25519),
                   (multi, addrbytes.TAG_MULTISIG)]:
        blob = addrbytes.pack_addr(s)
        assert blob[0] == tag, (s, len(s), blob[0], tag)
        assert addrbytes.unpack_addr(blob) == s


def test_addr_verbatim_sentinels_roundtrip():
    for s in ["genesis", "Hypernode", "0xABCDEF", "foo_bar_contract"]:
        blob = addrbytes.pack_addr(s)
        assert blob[0] == addrbytes.TAG_VERBATIM
        assert addrbytes.unpack_addr(blob) == s


def test_addr_leading_one_base58_preserved():
    # leading '1' base58 chars (leading zero bytes) must survive
    import base58
    s = "Bis1" + base58.b58encode(b"\x00\x00" + b"\x07" * 22).decode()
    if 32 <= len(s) <= 50:
        blob = addrbytes.pack_addr(s)
        assert addrbytes.unpack_addr(blob) == s


def test_addr_str_transition_guard():
    # a plain str value (pre-Stage-4 / mid-rollback row) reads back unchanged
    assert addrbytes.unpack_addr("Bis1legacyStringRow") == "Bis1legacyStringRow"


def test_addr_verbatim_length_bound():
    with pytest.raises(ValueError):
        addrbytes.pack_addr("x" * 256)        # > 255 utf-8 bytes (can't happen post [:56] truncation)
