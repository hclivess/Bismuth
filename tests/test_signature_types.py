"""
The polysign signature-scheme menu — REAL crypto round-trips for the signer families added alongside
ML-DSA-65: the full ML-DSA family (ML-DSA-44 / NIST Cat 2 and ML-DSA-87 / NIST Cat 5) and the classical
P-256 / secp256r1 ECDSA scheme (passkeys / hardware secure-elements). See doc/09 and doc/20.

Each new signer gets the same battery the ML-DSA-65 signer gets in tests/test_pq_signer.py:
  * a sign / verify round-trip,
  * deterministic-from-seed keys that survive a seed reload,
  * a short hash-of-pubkey address (< 70 chars), and
  * rejection of a tampered signature.

These exercise the signers both directly and through SignerFactory.signer_for_type. They are pure-crypto
unit tests; they do NOT need the regnet node fixture. ML-DSA needs dilithium_py, secp256r1 needs
cryptography — both are skipped if the (lazy, optional) dependency is missing.

Run with: python3 -m pytest tests/test_signature_types.py -v
"""
from base64 import b64encode

import pytest

from polysign.signer import SignerType
from polysign.signerfactory import signer_for_type


def _pubkey_bytes(signer):
    """Raw public-key bytes for a signer, regardless of the family's internal attribute."""
    pk = getattr(signer, "_pk", None)
    if pk is not None:
        return pk                       # ML-DSA family keeps the raw pubkey in _pk
    return bytes.fromhex(signer._public_key)


# (label, SignerType, expected raw signature length, expected raw pubkey length)
# Sizes are FIPS 204 fixed for ML-DSA; secp256r1 ECDSA DER sigs vary (~70-72 B), checked loosely below.
_ML_DSA_CASES = [
    ("MLDSA44", SignerType.MLDSA44, 2420, 1312),   # NIST Cat 2 (Dilithium2)
    ("MLDSA87", SignerType.MLDSA87, 4627, 2592),   # NIST Cat 5 (Dilithium5)
]


def _make(signer_type, seed="bismuth-sigtype-seed"):
    cls = signer_for_type(signer_type)
    assert cls is not None, f"no signer registered for {signer_type}"
    s = cls()
    s.from_seed(seed)
    return cls, s


# --------------------------------------------------------------------------- ML-DSA 44 / 87

@pytest.mark.parametrize("label,signer_type,sig_len,pk_len", _ML_DSA_CASES)
def test_mldsa_sign_verify_roundtrip(label, signer_type, sig_len, pk_len):
    pytest.importorskip("dilithium_py")
    cls, s = _make(signer_type)
    assert s._type == signer_type
    msg = b"move 10 BIS to alice"
    sig = s.sign_buffer_raw(msg)
    assert len(sig) == sig_len                      # fixed FIPS 204 signature size for this level
    assert len(_pubkey_bytes(s)) == pk_len          # fixed FIPS 204 pubkey size for this level
    cls.verify_signature(sig, _pubkey_bytes(s), msg, address=s.address())   # raises if invalid


@pytest.mark.parametrize("label,signer_type,sig_len,pk_len", _ML_DSA_CASES)
def test_mldsa_deterministic_from_seed_and_reload(label, signer_type, sig_len, pk_len):
    pytest.importorskip("dilithium_py")
    cls, a = _make(signer_type, "same-seed")
    _, b = _make(signer_type, "same-seed")
    assert a.address() == b.address() and a._public_key == b._public_key
    _, other = _make(signer_type, "other-seed")
    assert other.address() != a.address()
    # the wallet key is a 32-byte seed; re-loading it reproduces the keypair
    reloaded = cls()
    reloaded.from_private_key(a._private_key)
    assert reloaded.address() == a.address()
    assert reloaded._public_key == a._public_key


@pytest.mark.parametrize("label,signer_type,sig_len,pk_len", _ML_DSA_CASES)
def test_mldsa_hash_address_is_short(label, signer_type, sig_len, pk_len):
    pytest.importorskip("dilithium_py")
    cls, s = _make(signer_type)
    # large pubkey -> the address must commit to a HASH of it, not embed it
    assert len(_pubkey_bytes(s)) == pk_len
    assert s.address() == cls.public_key_to_address(_pubkey_bytes(s))
    assert len(s.address()) < 70                    # stays short (~54 chars), unlike the multi-KB pubkey


@pytest.mark.parametrize("label,signer_type,sig_len,pk_len", _ML_DSA_CASES)
def test_mldsa_tampered_signature_rejected(label, signer_type, sig_len, pk_len):
    pytest.importorskip("dilithium_py")
    cls, s = _make(signer_type)
    msg = b"pay bob"
    sig = bytearray(s.sign_buffer_raw(msg))
    sig[100] ^= 0xFF
    with pytest.raises(ValueError):
        cls.verify_signature(bytes(sig), _pubkey_bytes(s), msg, address=s.address())


@pytest.mark.parametrize("label,signer_type,sig_len,pk_len", _ML_DSA_CASES)
def test_mldsa_bis_network_format_roundtrip(label, signer_type, sig_len, pk_len):
    pytest.importorskip("dilithium_py")
    cls, s = _make(signer_type)
    msg = b"network format"
    cls.verify_bis_signature(s.sign_buffer_for_bis(msg),
                             b64encode(_pubkey_bytes(s)).decode(), msg, address=s.address())


# --------------------------------------------------------------------------- secp256r1 (P-256)

def _r1():
    pytest.importorskip("cryptography")
    return _make(SignerType.SECP256R1)


def test_secp256r1_sign_verify_roundtrip():
    cls, s = _r1()
    assert s._type == SignerType.SECP256R1
    msg = b"move 10 BIS to alice"
    sig = s.sign_buffer_raw(msg)
    assert 8 <= len(sig) <= 80                       # DER-encoded ECDSA signature (~70-72 B)
    assert len(_pubkey_bytes(s)) == 65               # uncompressed X9.62 point
    cls.verify_signature(sig, _pubkey_bytes(s), msg, address=s.address())   # raises if invalid


def test_secp256r1_deterministic_from_seed_and_reload():
    cls, a = _r1()
    _, a_again = _make(SignerType.SECP256R1, "bismuth-sigtype-seed")
    assert a.address() == a_again.address() and a._public_key == a_again._public_key
    _, other = _make(SignerType.SECP256R1, "other-seed")
    assert other.address() != a.address()
    # the wallet key is a 32-byte seed; re-loading it reproduces the keypair
    reloaded = cls()
    reloaded.from_private_key(a._private_key)
    assert reloaded.address() == a.address()
    assert reloaded._public_key == a._public_key


def test_secp256r1_hash_address_is_short():
    cls, s = _r1()
    assert s.address() == cls.public_key_to_address(_pubkey_bytes(s))
    assert len(s.address()) < 70                      # short hash-of-pubkey address (~54 chars)


def test_secp256r1_tampered_signature_rejected():
    cls, s = _r1()
    msg = b"pay bob"
    sig = bytearray(s.sign_buffer_raw(msg))
    sig[-1] ^= 0xFF                                   # corrupt the trailing s-value byte
    with pytest.raises(ValueError):
        cls.verify_signature(bytes(sig), _pubkey_bytes(s), msg, address=s.address())


def test_secp256r1_bis_network_format_roundtrip():
    cls, s = _r1()
    msg = b"network format"
    cls.verify_bis_signature(s.sign_buffer_for_bis(msg),
                             b64encode(_pubkey_bytes(s)).decode(), msg, address=s.address())


# --------------------------------------------------------------------------- menu wiring

def test_factory_resolves_every_new_type_to_distinct_class():
    """The factory must map each new SignerType to its own registered class."""
    pytest.importorskip("dilithium_py")
    pytest.importorskip("cryptography")
    from polysign.signer_mldsa import SignerMLDSA44, SignerMLDSA65, SignerMLDSA87, SignerMLDSA
    from polysign.signer_secp256r1 import SignerSECP256R1
    assert SignerMLDSA is SignerMLDSA65                          # back-compat alias preserved
    assert signer_for_type(SignerType.MLDSA44) is SignerMLDSA44
    assert signer_for_type(SignerType.MLDSA87) is SignerMLDSA87
    assert signer_for_type(SignerType.SECP256R1) is SignerSECP256R1
    # MLDSA and the explicit MLDSA65 alias both resolve to the Cat-3 class
    assert signer_for_type(SignerType.MLDSA) is SignerMLDSA65
    assert signer_for_type(SignerType.MLDSA65) is SignerMLDSA65


def test_new_address_version_prefixes_are_distinct():
    """Each family carries its own 4-byte mainnet address version so addresses are self-identifying."""
    pytest.importorskip("dilithium_py")
    pytest.importorskip("cryptography")
    from polysign.signer import SignerSubType
    types = [SignerType.MLDSA44, SignerType.MLDSA65, SignerType.MLDSA87, SignerType.SECP256R1]
    versions = {signer_for_type(t).address_version_for_subtype(SignerSubType.MAINNET_REGULAR) for t in types}
    assert len(versions) == len(types)               # all four prefixes are distinct
    for v in versions:
        assert len(v) == 4
