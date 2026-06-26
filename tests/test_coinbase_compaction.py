"""hf2 §2.C coinbase compaction: the mining-reward (coinbase) tx carries NO signature + public key
post-fork (authorized by PoW + the reward formula), and ANY address scheme may mine.

Run with: python3 -m pytest tests/test_coinbase_compaction.py -v
"""
import pytest

from polysign.signerfactory import SignerFactory


def _v(post_fork, address, sig, pub, is_coinbase):
    SignerFactory.verify_tx_signature(post_fork, "1750000000.00", address, address, "0.00000000",
                                      "0", "nonce", sig, pub, is_coinbase=is_coinbase)


def test_postfork_coinbase_empty_sig_accepted_any_scheme():
    # PoW + reward formula authorize the coinbase; the per-scheme verifier is skipped. Any address mines.
    import base58
    rsa = "ab" * 28                                   # 56-hex RSA
    ec = "Bis1" + base58.b58encode(bytes([7]) * 24).decode()
    multi = "Bism" + base58.b58encode(bytes([7]) * 24).decode()
    for addr in (rsa, ec, multi):
        _v(True, addr, "", "", is_coinbase=True)      # empty sig+pubkey -> accepted, no raise


def test_postfork_coinbase_must_be_empty():
    # a coinbase that smuggles a signature or pubkey post-fork is rejected (defense-in-depth)
    with pytest.raises(ValueError, match="empty signature and public key"):
        _v(True, "ab" * 28, "SOMESIG", "", is_coinbase=True)
    with pytest.raises(ValueError, match="empty signature and public key"):
        _v(True, "ab" * 28, "", "SOMEPUB", is_coinbase=True)


def test_prefork_coinbase_unaffected():
    # pre-fork the coinbase is NOT given the empty-sig carve-out (is_coinbase only matters post-fork);
    # it goes through the legacy verifier like any tx (here it would try to verify the bogus sig -> raise,
    # proving the carve-out did not fire pre-fork).
    with pytest.raises(Exception):
        _v(False, "ab" * 28, "notarealsig", "notarealpub", is_coinbase=True)


def test_block_hash_v2_omits_coinbase_sig():
    # the §2.C pre-image drops the coinbase (last tx) sig+pubkey -> hash invariant to them
    import bismuth_serialize as B
    normal = ("1750000000.00", "a", "r", "1.00000000", "s", "p", "op", "of")
    cb_a = ("1750000000.00", "m", "m", "0.00000000", "SIGA", "PUBA", "0", "nonce")
    cb_b = ("1750000000.00", "m", "m", "0.00000000", "", "", "0", "nonce")
    assert B.block_hash_v2([normal, cb_a], "ab" * 32) == B.block_hash_v2([normal, cb_b], "ab" * 32)
