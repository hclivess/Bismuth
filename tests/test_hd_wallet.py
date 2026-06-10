"""HD wallet (hd_wallet.py) — BIP32-style deterministic addresses for Bismuth.

OFFLINE tests (no node): derivation determinism + uniqueness, path parsing, hardened vs normal, child
pubkey consistency, node-format sign/verify through the REAL SignerFactory, wrong-key rejection,
passphrase reproducibility, invalid-index skipping.
LIVE regnet test: fund a freshly derived address from the RSA wallet, then SPEND from it with its
HD-derived ECDSA key — proving derived addresses are real and spendable end-to-end.

Run with: python3 -m pytest tests/test_hd_wallet.py -v
"""
from time import sleep

import pytest

import bismuth_serialize
import hd_wallet
from hd_wallet import HDWallet, HDNode, InvalidKeyError
from polysign.signer_ecdsa import SignerECDSA
from polysign.signerfactory import SignerFactory

SEED = bytes(range(32))           # a fixed 32-byte seed


# ----------------------------------------------------------------------------- offline (no node)
def test_addresses_are_valid_bismuth_ecdsa():
    w = HDWallet(SEED)
    for i in range(5):
        addr = w.receive_address(i)
        assert addr.startswith("Bis1") and 32 <= len(addr) <= 50
        assert SignerFactory.address_to_signer(addr) is SignerECDSA, addr


def test_derivation_is_deterministic():
    a = HDWallet(SEED)
    b = HDWallet(SEED)
    assert [a.receive_address(i) for i in range(8)] == [b.receive_address(i) for i in range(8)]


def test_distinct_indices_and_accounts_give_distinct_addresses():
    w = HDWallet(SEED)
    ext = {w.receive_address(i) for i in range(16)}
    assert len(ext) == 16, "receive addresses must be unique per index"
    assert w.receive_address(0, account=0) != w.receive_address(0, account=1)
    assert w.node_at(0, change=0).address() != w.node_at(0, change=1).address()


def test_different_seed_gives_different_wallet():
    assert HDWallet(SEED).receive_address(0) != HDWallet(bytes([1]) + SEED[1:]).receive_address(0)


def test_path_parsing_matches_helpers():
    w = HDWallet(SEED)
    # the helper path equals an explicit derive_path
    n1 = w.node_at(5, account=2, change=0)
    n2 = w.master.derive_path("m/44'/%d'/2'/0/5" % w.coin_type)
    assert n1.private_hex == n2.private_hex
    assert w.master.derive_path("m/0H/1h/2'").private_hex == w.master.child(hd_wallet.HARDENED).child(
        1 + hd_wallet.HARDENED).child(2 + hd_wallet.HARDENED).private_hex


def test_child_pubkey_consistency():
    """The derived child private key's public key matches re-deriving the address from the public key."""
    w = HDWallet(SEED)
    node = w.node_at(3)
    signer = node.signer()
    assert signer.address() == SignerECDSA.public_key_to_address(node.public_key_compressed)
    assert signer._public_key == node.public_key_hex


def test_sign_verifies_through_node_factory():
    w = HDWallet(SEED)
    node = w.node_at(7)
    signer = node.signer()
    tx = hd_wallet.sign_transaction(signer, "1700000000.00", signer.address(),
                                    "Bis1recipientxxxxxxxxxxxxxxxxx", 1.25, "op", "memo")
    buf = bismuth_serialize.signature_buffer(tx[0], tx[1], tx[2], tx[3], tx[6], tx[7])
    # the node's exact acceptance check
    SignerFactory.verify_bis_signature(tx[4], tx[5], buf, tx[1])


def test_wrong_key_cannot_sign_for_address():
    w = HDWallet(SEED)
    victim = w.node_at(1).signer()
    attacker = w.node_at(2).signer()
    buf = bismuth_serialize.signature_buffer("1700000000.00", victim.address(),
                                             "Bis1xxxxxxxxxxxxxxxxxxxxxxxxxx", "%.8f" % 1.0, "", "")
    bad_sig = attacker.sign_buffer_for_bis(buf)
    with pytest.raises(ValueError):
        SignerFactory.verify_bis_signature(bad_sig, hd_wallet.tx_public_key_b64(attacker), buf,
                                           victim.address())   # attacker sig, victim address -> reject


def test_from_passphrase_is_reproducible_and_distinct():
    a = HDWallet.from_passphrase("correct horse battery staple")
    b = HDWallet.from_passphrase("correct horse battery staple")
    c = HDWallet.from_passphrase("correct horse battery stapler")
    assert a.receive_address(0) == b.receive_address(0)
    assert a.receive_address(0) != c.receive_address(0)


def test_addresses_generator_skips_invalid(monkeypatch):
    w = HDWallet(SEED)
    # force the leaf at change/index to raise once, proving the generator advances past invalid indices
    real_child = HDNode.child
    calls = {"n": 0}

    def flaky(self, index):
        # raise only for the external-chain leaf index 0 (depth 5), once
        if self.depth == 4 and (index & 0xFFFFFFFF) == 0 and calls["n"] == 0:
            calls["n"] += 1
            raise InvalidKeyError("forced")
        return real_child(self, index)

    monkeypatch.setattr(HDNode, "child", flaky)
    idxs = [i for i, _ in w.addresses(3, change=0, start=0)]
    assert 0 not in idxs and len(idxs) == 3, idxs   # index 0 skipped, three valid addresses still produced


def test_invalid_node_rejected():
    with pytest.raises(ValueError):
        HDNode(b"\x00" * 32, b"\x11" * 32)            # zero private key
    with pytest.raises(ValueError):
        HDNode((hd_wallet._N).to_bytes(32, "big"), b"\x11" * 32)   # >= n


# ----------------------------------------------------------------------------- live regnet
def test_hd_address_is_spendable(client):
    client.mine(2)
    w = HDWallet(b"\x99" * 32)
    node = w.node_at(0)
    addr = node.address()

    # the RSA wallet funds the freshly derived HD address
    client.send(addr, 5, "", "")
    client.mine(2)
    sleep(0.3)
    assert abs(client.balance(addr) - 5) < 1e-9, "derived HD address not funded"

    # the HD-derived ECDSA key spends from it to a second derived address
    dest = w.node_at(1).address()
    client.send_with_signer(dest, 3, node.signer(), "", "hd-spend")
    client.mine(2)
    sleep(0.3)
    assert abs(client.balance(dest) - 3) < 1e-9, "HD address could not spend to another derived address"
    assert client.balance(addr) < 2.001, "sender HD address not debited (amount + fee)"
