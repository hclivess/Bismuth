"""hf2 Ethereum-shape single-sig: content-hash txid + recoverable secp256k1 + ecrecover (no pubkey).

Node-free unit + adversarial coverage of the new crypto path (the fork-transition end-to-end test lives
in test_hf2_fork_transition.py). Run: python3 -m pytest tests/test_hf2_recoverable.py -v
"""
import hashlib

import pytest

import bismuth_serialize
import hd_wallet
from coincurve.utils import GROUP_ORDER_INT
from polysign.signer import SignerType
from polysign.signer_ecdsa import SignerECDSA
from polysign.signerfactory import SignerFactory


def _ecdsa_signer(seed="a" * 64):
    s = SignerFactory.from_seed(seed, signer_type=SignerType.ECDSA)
    return s


def _fields(addr, recipient):
    return ("1718000000.00", addr, recipient, "1.00000000", "", "")


# --- spec / determinism --------------------------------------------------------------------------

def test_txid_is_blake2b256_of_signature_buffer():
    f = ("1718000000.00", "Bis1abc", "Bis1def", "1.00000000", "", "")
    expected = hashlib.blake2b(bismuth_serialize.signature_buffer(*f), digest_size=32).hexdigest()
    got = bismuth_serialize.tx_id(*f)
    assert got == expected
    assert len(got) == 64 and got == got.lower()
    # excludes signature/pubkey by construction (only 6 args) and is stable
    assert bismuth_serialize.tx_id(*f) == got
    assert bismuth_serialize.signed_message(got) == bytes.fromhex(got)
    assert len(bismuth_serialize.signed_message(got)) == 32


def test_single_sig_ecdsa_detection():
    s = _ecdsa_signer()
    assert SignerFactory.is_single_sig_ecdsa(s.address()) is True
    # an RSA (56-hex) address and a multisig (Bism...) address are NOT single-sig ecdsa
    assert SignerFactory.is_single_sig_ecdsa("a" * 56) is False
    assert SignerFactory.is_single_sig_ecdsa("Bism" + "1" * 33) is False


# --- happy path: round-trip sign -> ecrecover -> address ------------------------------------------

def test_recoverable_roundtrip_and_dropped_pubkey():
    s = _ecdsa_signer()
    addr = s.address()
    tx = hd_wallet.sign_transaction(s, "1718000000.00", addr, addr, "1.0", post_fork=True)
    # public_key field dropped, signature is 65-byte recoverable hex (130 chars, lowercase)
    assert tx[5] == ""
    assert len(tx[4]) == 130 and tx[4] == tx[4].lower()
    # verifies through the single fork-aware entry point with post_fork=True
    SignerFactory.verify_tx_signature(True, tx[0], tx[1], tx[2], tx[3], tx[6], tx[7], tx[4], tx[5])


def test_post_fork_dispatch_rejects_legacy_signed_tx():
    # a legacy (buffer+pubkey, base64 DER) single-sig tx must NOT verify under the post-fork ecrecover path
    s = _ecdsa_signer()
    addr = s.address()
    legacy = hd_wallet.sign_transaction(s, "1718000000.00", addr, addr, "1.0", post_fork=False)
    assert legacy[5] != ""   # legacy carries the pubkey
    with pytest.raises(ValueError):
        SignerFactory.verify_tx_signature(True, legacy[0], legacy[1], legacy[2], legacy[3],
                                          legacy[6], legacy[7], legacy[4], legacy[5])


def test_pre_fork_dispatch_rejects_recoverable_tx():
    # and the reverse: a recoverable (no-pubkey) tx must NOT verify under the legacy buffer path
    s = _ecdsa_signer()
    addr = s.address()
    tx = hd_wallet.sign_transaction(s, "1718000000.00", addr, addr, "1.0", post_fork=True)
    with pytest.raises(Exception):
        SignerFactory.verify_tx_signature(False, tx[0], tx[1], tx[2], tx[3], tx[6], tx[7], tx[4], tx[5])


# --- adversarial ---------------------------------------------------------------------------------

def test_reject_high_s_malleable():
    # s in (N/2, N) is non-canonical; must be rejected BEFORE recovery (malleability guard)
    r = (1).to_bytes(32, "big")
    s_high = (GROUP_ORDER_INT - 1).to_bytes(32, "big")
    sig = (r + s_high + bytes([0])).hex()
    with pytest.raises(ValueError, match="non-canonical"):
        SignerECDSA.verify_bis_signature_recovered(sig, "ab" * 32, "Bis1whatever")


def test_reject_zero_s():
    sig = ((1).to_bytes(32, "big") + (0).to_bytes(32, "big") + bytes([0])).hex()
    with pytest.raises(ValueError, match="non-canonical"):
        SignerECDSA.verify_bis_signature_recovered(sig, "ab" * 32, "Bis1whatever")


def test_reject_bad_length_and_recid():
    txid = bismuth_serialize.tx_id(*_fields("Bis1a", "Bis1b"))
    with pytest.raises(ValueError, match="length"):
        SignerECDSA.verify_bis_signature_recovered("aa" * 10, txid, "Bis1x")
    bad_recid = ((1).to_bytes(32, "big") + (2).to_bytes(32, "big") + bytes([7])).hex()
    with pytest.raises(ValueError, match="recovery id"):
        SignerECDSA.verify_bis_signature_recovered(bad_recid, txid, "Bis1x")


def test_reject_wrong_sender_address():
    # sign with key A but claim a different sender B -> recovered address != B -> reject (forgery guard)
    a = _ecdsa_signer("a" * 64)
    b = _ecdsa_signer("b" * 64)
    txid = bismuth_serialize.tx_id(*_fields(b.address(), b.address()))
    sig = a.sign_buffer_for_bis_recoverable(bytes.fromhex(txid))
    with pytest.raises(ValueError, match="wrong address"):
        SignerECDSA.verify_bis_signature_recovered(sig, txid, b.address())


def test_reject_content_mismatch():
    # valid sig over txid of fields X, but verify against fields Y -> recovered addr won't match sender
    s = _ecdsa_signer()
    addr = s.address()
    txid_x = bismuth_serialize.tx_id("1718000000.00", addr, addr, "1.00000000", "", "")
    sig = s.sign_buffer_for_bis_recoverable(bytes.fromhex(txid_x))
    txid_y = bismuth_serialize.tx_id("1718000000.00", addr, addr, "2.00000000", "", "")  # amount changed
    assert txid_x != txid_y
    with pytest.raises(ValueError):
        SignerECDSA.verify_bis_signature_recovered(sig, txid_y, addr)


def test_reject_non_hex_signature():
    with pytest.raises(ValueError, match="hex"):
        SignerECDSA.verify_bis_signature_recovered("not-hex-!!", "ab" * 32, "Bis1x")
