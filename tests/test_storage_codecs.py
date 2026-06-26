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


# --------------------------------------------------------------------------- tx-fields (numeric) codec
import amounts
import txfields


def test_timestamp_roundtrip_byte_identical():
    for ts in ["1750000000.00", "1600000000.01", "0.00", "1234567890.99"]:
        blob = txfields.pack_timestamp(ts)
        assert txfields.unpack_timestamp(blob) == ts          # exact '%.2f' reconstruction


@pytest.mark.parametrize("s", ["1.00000000", "0.01000000", "5.00000000", "0.50000000",
                               "99999999.99999999", "0.00000001"])
def test_num_decimal_canonical_roundtrip(s):
    blob = txfields.pack_num(s)
    assert txfields.unpack_num(blob) == s                      # '%.8f' forms are byte-identical


def test_num_zero_normalizes_consensus_safe():
    # '0' is normalized to '0.00000000' — consensus-safe (both -> 0 units -> identical hash/txid)
    blob = txfields.pack_num("0")
    got = txfields.unpack_num(blob)
    assert got == "0.00000000"
    assert amounts.to_units(got) == amounts.to_units("0") == 0   # consensus equivalence


def test_num_integer_storage_mode_roundtrip():
    # in LEDGER_INTEGER mode the stored field IS a bare units string; to_units must NOT be applied
    orig = amounts.LEDGER_INTEGER
    amounts.LEDGER_INTEGER = True
    try:
        for s in ["0", "1", "500000000", "9007199254740993"]:
            blob = txfields.pack_num(s)
            assert txfields.unpack_num(blob) == s             # bare-units string round-trips exactly
    finally:
        amounts.LEDGER_INTEGER = orig


def test_varint_minimal_encoding_enforced():
    assert txfields.uvarint_decode(txfields.uvarint_encode(300)) == (300, 2)
    with pytest.raises(ValueError):
        txfields.uvarint_decode(b"\x80\x00")                  # non-minimal (overlong) zero


# --------------------------------------------------------------------------- difficulty / work record
import diff_store
import diff_work


def test_work_units_deterministic_and_monotone():
    assert diff_work.work_units(diff_store.diff_to_e10("16")) == 256        # 2**8
    assert diff_work.work_units(diff_store.diff_to_e10("50")) == 33554432   # 2**25
    assert diff_work.work_units(diff_store.diff_to_e10("100")) == 1125899906842624  # 2**50
    assert diff_work.work_units(0) == 0
    # strictly increasing in difficulty
    ws = [diff_work.work_units(diff_store.diff_to_e10(str(d))) for d in (16, 24, 50, 100)]
    assert ws == sorted(ws) and len(set(ws)) == 4


def test_diff_e10_roundtrip_grid():
    for s in ("16.0", "24", "50.0", "118.4732615334", "0.0000000001"):
        e10 = diff_store.diff_to_e10(s)
        from decimal import Decimal
        assert diff_store.e10_to_diff(e10) == Decimal(s).quantize(Decimal("0.0000000001"))


def test_diff_record_roundtrip_fixed_head():
    blob, cw = diff_store.make_record("118.4732615334", prev_ts=1000.0, this_ts=1063.0,
                                      prev_cumulative_work=999)
    assert len(blob) == diff_store.HEAD_LEN               # clean fixed 28-byte record, no legacy tail
    rec = diff_store.unpack_record(blob)
    assert rec["difficulty_e10"] == diff_store.diff_to_e10("118.4732615334")
    assert rec["solvetime"] == 63
    assert rec["cumulative_work"] == cw == 999 + diff_work.work_units(rec["difficulty_e10"])


def test_diff_text_rendered_from_integer_grid():
    # post-fork the canonical difficulty string is generated from difficulty_e10 (no legacy '16' vs '16.0')
    blob = diff_store.pack_record(diff_store.diff_to_e10("16.0"), 60, 1)
    assert diff_store.difficulty_text(blob) == "16.0000000000"
    assert int(float(diff_store.difficulty_text(blob))) == 16   # consensus reads int(float(...)), safe


def test_diff_cumulative_work_u128():
    big = (1 << 100) + 7
    blob = diff_store.pack_record(diff_store.diff_to_e10("100"), 60, big)
    assert diff_store.unpack_record(blob)["cumulative_work"] == big   # u128 limbs round-trip


# --------------------------------------------------------------------------- binary sync transport codec
import sync_codec


def test_sync_codec_roundtrip_byte_identical():
    import base58
    rsa_addr = "ab" * 28                       # 56-hex RSA address
    ec_addr = "Bis1" + base58.b58encode(bytes([7]) * 24).decode()
    blocks = [
        {"block_height": 26, "block_hash": "%064x" % 0xabc,   # post-fork 64-hex blake2b
         "transactions": [
             ["1750000000.00", rsa_addr, ec_addr, "1.23456789",
              base64.b64encode(b"\x07" * 64).decode(), base64.b64encode(b"\x09" * 140).decode(),
              "", "hello-openfield"],
             ["1750000001.50", ec_addr, rsa_addr, "0.00000000",
              bytes(range(65)).hex(), "", "vm:call", ""],   # recoverable-hex sig, dropped pubkey, op set
         ]},
        {"block_height": 27, "block_hash": "cd" * 28,        # pre-fork 56-hex sha224
         "transactions": [
             ["1750000099.00", rsa_addr, rsa_addr, "9000000.50000000",
              base64.b64encode(b"\x01" * 64).decode(), base64.b64encode(b"\x02" * 140).decode(), "", "x"],
         ]},
    ]
    blob = sync_codec.encode_blocks(blocks)
    assert blob[:4] == sync_codec.MAGIC
    got = sync_codec.decode_blocks(blob)
    assert got == blocks                         # byte-identical reconstruction (digester-safe)


def test_sync_codec_smaller_than_json():
    import json
    rsa_addr = "ab" * 28
    blocks = [{"block_height": h, "block_hash": "%064x" % h,
               "transactions": [["1750000000.00", rsa_addr, rsa_addr, "1.00000000",
                                 base64.b64encode(b"\x07" * 64).decode(),
                                 base64.b64encode(b"\x09" * 140).decode(), "", ""]] * 5}
              for h in range(50)]
    blob = sync_codec.encode_blocks(blocks)
    js = json.dumps({"blocks": blocks}).encode()
    assert len(blob) < len(js) * 0.75            # binary ~0.6-0.72 of JSON uncompressed (more for RSA pubkeys)


# --------------------------------------------------------------------------- txrec single-blob row codec
import txrec


def test_txrec_row_roundtrip():
    import base58
    ec = "Bis1" + base58.b58encode(bytes([7]) * 24).decode()
    rows = [
        # 11-field stored row: ts, addr, recip, amount, sig, pubkey_id(int), block_hash, fee, reward, op, openfield
        ["1750000000.00", "ab" * 28, ec, "1.23456789",
         base64.b64encode(b"\x07" * 64).decode(), 5, "%064x" % 7, "0.01000000", "0", "", "of"],
        ["1750000001.50", ec, "ab" * 28, "0",                       # amount '0' normalizes
         bytes(range(65)).hex(), 0, "hash-not-hex", "0", "5.00000000", "vm:call", ""],  # recoverable sig, non-hex bh
    ]
    for r in rows:
        blob = txrec.pack_row(r)
        got = txrec.unpack_row(blob)
        assert got[1] == r[1] and got[2] == r[2]           # address / recipient exact
        assert got[5] == r[5]                              # pubkey dedup id
        assert got[4] == r[4]                              # signature wire form (base64 / recoverable hex)
        assert got[9] == r[9] and got[10] == r[10]         # operation / openfield
        assert amounts.to_units(got[3]) == amounts.to_units(r[3])   # amount consensus-equivalent
    # block_hash is NOT stored in the blob (hoisted to the block envelope) -> None placeholder; block_store
    # ._expand fills it from the envelope hash, identical for every tx in the block.
    assert txrec.unpack_row(txrec.pack_row(rows[0]))[6] is None
    assert txrec.openfield_of(txrec.pack_row(rows[0])) == "of"
