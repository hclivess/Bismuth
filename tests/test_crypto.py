# Crypto / signature / fee / quantizer tests. Pure (no running node needed).
# Run with: python3 -m pytest -v

import base64
import hashlib
import time

import pytest
from Cryptodome.PublicKey import RSA

import essentials
import simplecrypt
from quantizer import quantize_two, quantize_eight, quantize_ten
from polysign.signerfactory import SignerFactory, _load_optional_signer
from polysign.signer import SignerType


@pytest.fixture(scope="module")
def rsa_identity():
    key = RSA.generate(1024)  # fast; the node accepts 1024 (legacy) and 4096
    pub_pem = key.publickey().exportKey().decode("utf-8")
    pub_b64 = base64.b64encode(pub_pem.encode("utf-8"))
    address = hashlib.sha224(pub_pem.encode("utf-8")).hexdigest()
    return key, pub_pem, pub_b64, address


def test_address_is_56_hex(rsa_identity):
    address = rsa_identity[3]
    assert len(address) == 56
    assert all(c in "0123456789abcdef" for c in address)


def test_address_validation(rsa_identity):
    address = rsa_identity[3]
    assert SignerFactory.address_is_valid(address)
    assert SignerFactory.address_is_rsa(address)
    assert not SignerFactory.address_is_valid("not-an-address")
    assert not SignerFactory.address_is_valid("z" * 56)  # right length, not hex


def test_sign_and_verify_roundtrip(rsa_identity):
    key, _, pub_b64, address = rsa_identity
    ts = "%.2f" % time.time()
    signed = essentials.sign_rsa(ts, address, address, 1.23456789, "op", "data", key, pub_b64)
    assert isinstance(signed, tuple) and len(signed) == 8
    buffer = str((ts, address, address, "%.8f" % 1.23456789, "op", "data")).encode("utf-8")
    SignerFactory.verify_bis_signature(signed[4], pub_b64.decode(), buffer, address)  # raises on failure


def test_tampered_signature_rejected(rsa_identity):
    key, _, pub_b64, address = rsa_identity
    ts = "%.2f" % time.time()
    signed = essentials.sign_rsa(ts, address, address, 1.0, "", "", key, pub_b64)
    bad_buffer = str((ts, address, address, "%.8f" % 2.0, "", "")).encode("utf-8")  # amount changed
    with pytest.raises(Exception):
        SignerFactory.verify_bis_signature(signed[4], pub_b64.decode(), bad_buffer, address)


def test_wallet_der_roundtrip(tmp_path, rsa_identity):
    key, pub_pem, pub_b64, address = rsa_identity
    priv_pem = key.exportKey().decode("utf-8")
    wallet = str(tmp_path / "wallet.der")
    essentials.keys_save(priv_pem, pub_pem, address, wallet)
    loaded = essentials.keys_load_new(wallet)
    assert loaded[6] == address      # address
    assert loaded[5] == pub_b64      # public key, base64


def test_simplecrypt_roundtrip():
    blob = simplecrypt.encrypt("pw", "secret")
    assert simplecrypt.decrypt("pw", blob).decode("utf-8") == "secret"
    with pytest.raises(Exception):
        simplecrypt.decrypt("wrong-password", blob)


def test_fee_formula():
    assert str(essentials.fee_calculate("")) == "0.01000000"
    assert str(essentials.fee_calculate("x" * 50)) == "0.01050000"
    assert str(essentials.fee_calculate("alias=bob")) == "1.01009000"
    assert str(essentials.fee_calculate("", "token:issue")) == "10.01000000"


def test_quantizer():
    assert str(quantize_two(1)) == "1.00"
    assert str(quantize_eight(1)) == "1.00000000"
    assert str(quantize_ten(1)) == "1.0000000000"
    assert str(quantize_eight(0)) in ("0E-8", "0.00000000")  # falsy -> zero Decimal


def test_polysign_lazy_ecdsa():
    # Non-RSA signers are lazy-loaded after reintegration; exercise the ECDSA path (needs coincurve).
    SignerECDSA = _load_optional_signer("SignerECDSA")
    assert SignerECDSA.__name__ == "SignerECDSA"
    signer = SignerFactory.from_seed(signer_type=SignerType.ECDSA)
    sig = signer.sign_buffer_for_bis(b"hello bismuth")
    assert sig  # a coincurve-backed ECDSA signature was produced via the lazily-loaded signer
