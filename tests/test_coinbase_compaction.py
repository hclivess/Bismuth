"""hf2 coinbase (§2.C + doc/41): the mining-reward (coinbase) tx is authorized by PoW + the reward
formula and is NEVER signature-verified, so ANY address scheme may mine. doc/41 repurposes its freed
signature/public_key slots as the mining header (slot4=PoW nonce, slot5="vmsr"<root> commitment) — they
are no longer required empty; the verifier treats them as opaque.

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


def test_postfork_coinbase_carries_mining_header():
    # doc/41: the post-fork coinbase REUSES its sig/pubkey slots as the mining header (slot4=nonce,
    # slot5="vmsr"<root> commitment). They are no longer required empty — the verifier accepts them as
    # opaque (the nonce is PoW-checked and the root enforced in digest.py). Recipient==address still holds.
    _v(True, "ab" * 28, "deadbeefnonce", "vmsr" + "ab" * 32, is_coinbase=True)   # mining header -> accepted
    _v(True, "ab" * 28, "", "", is_coinbase=True)                               # empty also still accepted


def test_postfork_coinbase_recipient_must_equal_miner():
    # the reward is credited to the PoW-bound miner address; a coinbase naming a different recipient is
    # malformed and rejected (defense-in-depth — the reward credits the address regardless).
    with pytest.raises(ValueError, match="recipient must equal the miner address"):
        SignerFactory.verify_tx_signature(True, "1750000000.00", "ab" * 28, "cd" * 28, "0.00000000",
                                          "0", "nonce", "", "", is_coinbase=True)


def test_prefork_coinbase_unaffected():
    # pre-fork the coinbase is NOT given the empty-sig carve-out (is_coinbase only matters post-fork);
    # it goes through the legacy verifier like any tx (here it would try to verify the bogus sig -> raise,
    # proving the carve-out did not fire pre-fork).
    with pytest.raises(Exception):
        _v(False, "ab" * 28, "notarealsig", "notarealpub", is_coinbase=True)


def test_block_hash_v2_commits_coinbase_mining_header():
    # doc/41: the coinbase sig/pubkey slots now carry the committed mining header (nonce, commitment), so
    # the block hash DOES depend on them — a forged nonce or state-root commitment changes the hash.
    import bismuth_serialize as B
    normal = ("1750000000.00", "a", "r", "1.00000000", "s", "p", "op", "of")
    cb_a = ("1750000000.00", "m", "m", "0.00000000", "NONCEA", "vmsrROOTA", "", "")
    cb_b = ("1750000000.00", "m", "m", "0.00000000", "NONCEB", "vmsrROOTB", "", "")
    assert B.block_hash_v2([normal, cb_a], "ab" * 32) != B.block_hash_v2([normal, cb_b], "ab" * 32)
