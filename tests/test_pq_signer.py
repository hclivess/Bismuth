"""
ML-DSA-65 (FIPS 204 / CRYSTALS-Dilithium3, NIST Cat 3) post-quantum signer
(polysign/signer_mldsa) — REAL crypto: sign/verify round-trips, deterministic-from-seed keys, and
hash-of-pubkey addresses. The post-quantum pivot as working, tested code (doc/20); inert on consensus
until a `pq` fork enables acceptance.

Run with: python3 -m pytest tests/test_pq_signer.py -v
"""
from base64 import b64encode

import pytest

pytest.importorskip("dilithium_py")

from polysign.signer import SignerType
from polysign.signer_mldsa import SignerMLDSA


def _signer(seed="bismuth-pq-test-seed"):
    s = SignerMLDSA()
    s.from_seed(seed)
    return s


def test_sign_verify_roundtrip():
    s = _signer()
    assert s._type == SignerType.MLDSA
    msg = b"move 10 BIS to alice"
    sig = s.sign_buffer_raw(msg)
    assert len(sig) == 3309                              # ML-DSA-65 signature size
    SignerMLDSA.verify_signature(sig, s._pk, msg, address=s.address())   # raises if invalid


def test_deterministic_from_seed():
    a, b = _signer("same-seed"), _signer("same-seed")
    assert a.address() == b.address() and a._public_key == b._public_key
    assert _signer("other-seed").address() != a.address()
    # the wallet key is a 32-byte seed; re-loading it reproduces the keypair
    reloaded = SignerMLDSA()
    reloaded.from_private_key(a._private_key)
    assert reloaded.address() == a.address()


def test_address_is_hash_of_pubkey():
    s = _signer()
    assert len(s._pk) == 1952                            # large pubkey -> the address commits to a hash
    assert s.address() == SignerMLDSA.public_key_to_address(s._pk)
    assert len(s.address()) < 60                          # ...and stays short (~55 chars), unlike the pubkey


def test_tampered_signature_rejected():
    s = _signer()
    msg = b"pay bob"
    sig = bytearray(s.sign_buffer_raw(msg))
    sig[100] ^= 0xFF
    with pytest.raises(ValueError):
        SignerMLDSA.verify_signature(bytes(sig), s._pk, msg, address=s.address())


def test_wrong_address_rejected():
    s, other = _signer("a"), _signer("b")
    msg = b"x"
    sig = s.sign_buffer_raw(msg)
    with pytest.raises(ValueError):                       # valid sig, but claimed against another address
        SignerMLDSA.verify_signature(sig, s._pk, msg, address=other.address())


def test_bis_network_format_roundtrip():
    s = _signer()
    msg = b"network format"
    SignerMLDSA.verify_bis_signature(s.sign_buffer_for_bis(msg), b64encode(s._pk).decode(), msg, address=s.address())
