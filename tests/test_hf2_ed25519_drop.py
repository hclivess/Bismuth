"""hf2 Stage 3: post-fork ED25519 drops the public key (recovered statelessly from the address).

Node-free unit coverage, mirroring test_hf2_recoverable for the secp256k1 path. The ED25519 key is
embedded in the address (base58(version+key+checksum)), so it need not ride on the tx; the verifier
recovers it. Run: python3 -m pytest tests/test_hf2_ed25519_drop.py -v
"""
import pytest

pytest.importorskip("ed25519")

import hd_wallet
from polysign.signer import SignerType
from polysign.signer_ed25519 import SignerED25519
from polysign.signerfactory import SignerFactory


def _ed(seed="a" * 64):
    return SignerFactory.from_seed(seed, signer_type=SignerType.ED25519)


def test_ed25519_address_detection_distinct_from_ecdsa():
    addr = _ed().address()
    assert SignerFactory.is_single_sig_ed25519(addr) is True
    assert SignerFactory.is_single_sig_ecdsa(addr) is False     # the Bis1 length split routes them apart


def test_pubkey_recovers_from_address_roundtrip():
    addr = _ed().address()
    key = SignerED25519.public_key_from_address(addr)
    assert len(key) == 32
    assert SignerED25519.public_key_to_address(key) == addr     # the recovered key rebuilds the address
    with pytest.raises(ValueError):
        SignerED25519.public_key_from_address("Bis1" + "1" * 60)  # garbage address -> rejected


def test_post_fork_ed25519_drops_pubkey_and_verifies():
    s = _ed()
    addr = s.address()
    tx = hd_wallet.sign_transaction(s, "1718000000.00", addr, addr, "1.0", post_fork=True)
    assert tx[5] == ""                                          # public key dropped on the wire
    # verifies through the single fork-aware entry point (recovers the key from the address)
    SignerFactory.verify_tx_signature(True, tx[0], tx[1], tx[2], tx[3], tx[6], tx[7], tx[4], tx[5])


def test_post_fork_ed25519_rejects_nonempty_pubkey():
    # a legacy (pubkey-carrying) ED25519 tx must NOT verify under the post-fork drop path (one canonical form)
    s = _ed()
    addr = s.address()
    legacy = hd_wallet.sign_transaction(s, "1718000000.00", addr, addr, "1.0", post_fork=False)
    assert legacy[5] != ""
    with pytest.raises(ValueError):
        SignerFactory.verify_tx_signature(True, legacy[0], legacy[1], legacy[2], legacy[3],
                                          legacy[6], legacy[7], legacy[4], legacy[5])


def test_pre_fork_ed25519_unchanged():
    # pre-fork: the legacy buffer+pubkey path verifies normally (no drop)
    s = _ed()
    addr = s.address()
    tx = hd_wallet.sign_transaction(s, "1718000000.00", addr, addr, "1.0", post_fork=False)
    assert tx[5] != ""
    SignerFactory.verify_tx_signature(False, tx[0], tx[1], tx[2], tx[3], tx[6], tx[7], tx[4], tx[5])
