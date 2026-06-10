"""BIP39 mnemonic codec (bip39.py) + HDWallet.from_mnemonic — offline, no node.

Correctness is pinned to the canonical Trezor BIP39 test vectors (passphrase "TREZOR"): if our
entropy->mnemonic->seed pipeline reproduces those published values byte-for-byte, a Bismuth phrase is
interoperable with any BIP39 tool. Plus: wordlist integrity, round-trip, checksum rejection, strengths,
passphrase effect, and that a restored wallet yields valid spendable Bismuth ECDSA addresses.

Run with: python3 -m pytest tests/test_bip39.py -v
"""
import pytest

import bip39
from hd_wallet import HDWallet
from polysign.signer_ecdsa import SignerECDSA
from polysign.signerfactory import SignerFactory

# (entropy_hex, mnemonic, seed_hex with passphrase "TREZOR") — official Trezor/BIP39 vectors
VECTORS = [
    ("00000000000000000000000000000000",
     "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
     "c55257c360c07c72029aebc1b53c05ed0362ada38ead3e3e9efa3708e53495531f09a6987599d18264c1e1c92f2cf1"
     "41630c7a3c4ab7c81b2f001698e7463b04"),
    ("7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f",
     "legal winner thank year wave sausage worth useful legal winner thank yellow",
     "2e8905819b8723fe2c1d161860e5ee1830318dbf49a83bd451cfb8440c28bd6fa457fe1296106559a3c80937a1c106"
     "9be3a3a5bd381ee6260e8d9739fce1f607"),
    ("80808080808080808080808080808080",
     "letter advice cage absurd amount doctor acoustic avoid letter advice cage above",
     "d71de856f81a8acc65e6fc851a38d4d7ec216fd0796d0a6827a3ad6ed5511a30fa280f12eb2e47ed2ac03b5c462a03"
     "58d18d69fe4f985ec81778c1b370b652a8"),
    ("0000000000000000000000000000000000000000000000000000000000000000",
     "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon "
     "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art",
     "bda85446c68413707090a52022edd26a1c9462295029f2e60cd7c4f2bbd3097170af7a4d73245cafa9c3cca8d561a7"
     "c3de6f5d4a10be8ed2a5e608d68f92fcc8"),
]


def test_wordlist_is_canonical():
    assert len(bip39.WORDLIST) == 2048
    assert bip39.WORDLIST[0] == "abandon" and bip39.WORDLIST[-1] == "zoo"
    assert bip39.verify_wordlist(), "on-disk wordlist is not the canonical BIP39 English list"


@pytest.mark.parametrize("ent_hex,mnemonic,seed_hex", VECTORS)
def test_official_vectors(ent_hex, mnemonic, seed_hex):
    # entropy -> mnemonic
    assert bip39.generate(len(ent_hex) * 4, entropy=bytes.fromhex(ent_hex)) == mnemonic
    # mnemonic -> entropy (round trip + checksum validates)
    assert bip39.to_entropy(mnemonic).hex() == ent_hex
    assert bip39.check(mnemonic)
    # mnemonic -> seed (interop: matches the published Trezor seed for passphrase "TREZOR")
    assert bip39.to_seed(mnemonic, "TREZOR").hex() == seed_hex


def test_roundtrip_random_strengths():
    import os
    for strength in (128, 160, 192, 224, 256):
        m = bip39.generate(strength)
        assert len(m.split()) == {128: 12, 160: 15, 192: 18, 224: 21, 256: 24}[strength]
        assert bip39.check(m)
        # deterministic from explicit entropy, and reversible
        ent = os.urandom(strength // 8)
        assert bip39.to_entropy(bip39.generate(strength, entropy=ent)) == ent


def test_checksum_and_word_rejection():
    good = VECTORS[0][1]
    assert bip39.check(good)
    # swap the last word for another valid word -> checksum breaks
    bad = good.rsplit(" ", 1)[0] + " zoo"
    assert not bip39.check(bad)
    with pytest.raises(ValueError):
        bip39.to_entropy(bad)
    # a non-wordlist token
    assert not bip39.check("abandon abandon notaword " + " ".join(["abandon"] * 9))
    # wrong length
    with pytest.raises(ValueError):
        bip39.to_entropy("abandon abandon about")


def test_passphrase_changes_seed_but_not_phrase():
    m = VECTORS[0][1]
    assert bip39.to_seed(m, "") != bip39.to_seed(m, "TREZOR")
    assert bip39.to_seed(m, "a") != bip39.to_seed(m, "b")


def test_invalid_strength_and_entropy():
    with pytest.raises(ValueError):
        bip39.generate(100)                          # not a valid strength
    with pytest.raises(ValueError):
        bip39.generate(128, entropy=b"\x00" * 8)     # length mismatch
    with pytest.raises(ValueError):
        bip39._entropy_to_mnemonic(b"\x00" * 17)     # not a valid entropy size


def test_hdwallet_from_mnemonic_is_deterministic_and_spendable():
    m = VECTORS[0][1]
    a = HDWallet.from_mnemonic(m)
    b = HDWallet.from_mnemonic(m)
    assert [a.receive_address(i) for i in range(4)] == [b.receive_address(i) for i in range(4)]
    # a BIP39 passphrase forks the wallet entirely
    assert a.receive_address(0) != HDWallet.from_mnemonic(m, "TREZOR").receive_address(0)
    # restored addresses are valid Bismuth ECDSA addresses the node would accept
    for i in range(3):
        addr = a.receive_address(i)
        assert addr.startswith("Bis1")
        assert SignerFactory.address_to_signer(addr) is SignerECDSA
        assert SignerFactory.address_is_valid(addr)


def test_from_mnemonic_rejects_bad_phrase():
    with pytest.raises(ValueError):
        HDWallet.from_mnemonic("abandon abandon abandon")          # too short / bad checksum


def test_new_mnemonic_creates_working_wallet():
    mnemonic, wallet = HDWallet.new_mnemonic(128)
    assert len(mnemonic.split()) == 12 and bip39.check(mnemonic)
    assert wallet.receive_address(0).startswith("Bis1")
    # the returned mnemonic restores the same wallet
    assert HDWallet.from_mnemonic(mnemonic).receive_address(0) == wallet.receive_address(0)
