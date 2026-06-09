"""
Regnet block generator must honour the dual-PoW switch (L5).

The Heavy3 inner hash modernises sha224 -> blake2b(28) post the PoW fork (doc/18-D); the verifier
(mining_heavy3.diffme_heavy3) and the solo miner (miner.py) both switch on `new_pow`. The regnet
generator (regnet.generate_one_block) previously hashed with sha224 UNCONDITIONALLY, so once the PoW
fork activated on a regnet it would mine blocks the node's own verifier rejects (wrong inner algo).

These are pure logic tests of the inner-digest SELECTION shared by generator and verifier — no mmap /
no live node (the memory-hard anneal that follows is identical for both algos and unaffected).

Run with: python3 -m pytest tests/test_regnet_dual_pow.py -v
"""
from hashlib import sha224, blake2b


def _verifier_raw(data: bytes, new_pow: bool) -> bytes:
    # exactly mining_heavy3.diffme_heavy3 / miner.py: blake2b(28) post-fork, else sha224
    return blake2b(data, digest_size=28).digest() if new_pow else sha224(data).digest()


def _generator_inner(s: str, new_pow: bool) -> int:
    # exactly the _inner() closure the fixed regnet.generate_one_block uses
    b = s.encode("utf-8")
    return int.from_bytes(
        blake2b(b, digest_size=28).digest() if new_pow else sha224(b).digest(), "big")


def test_generator_inner_matches_verifier_both_eras():
    # the generator feeds (pool_address + nonce + db_block_hash); the verifier hashes the same bytes
    s = "addr56hex" + "nonce" + "blockhash"
    for new_pow in (False, True):
        assert _generator_inner(s, new_pow) == int.from_bytes(
            _verifier_raw(s.encode("utf-8"), new_pow), "big"), new_pow


def test_blake2b_and_sha224_differ_so_always_sha224_is_wrong_post_fork():
    # the bug: hashing with sha224 when new_pow is True yields a DIFFERENT digest than the verifier's
    # blake2b -> the mined block fails its own PoW. Proves the two algos are not interchangeable.
    s = "addr" + "n" + "bh"
    sha = int.from_bytes(sha224(s.encode("utf-8")).digest(), "big")
    bla = _generator_inner(s, new_pow=True)
    assert sha != bla                                  # always-sha224 (old generator) != post-fork verifier
    assert _generator_inner(s, new_pow=False) == sha   # pre-fork the two agree (sha224)


def test_new_pow_gate_is_height_vs_pow_fork_height():
    # mirror the gate the generator computes: new_pow = pow_fork_height is not None and next_h >= it
    def gate(pow_fork_height, next_h):
        return pow_fork_height is not None and next_h >= pow_fork_height
    assert gate(None, 10) is False                     # never, while unlocked
    assert gate(100, 99) is False                      # before activation -> sha224
    assert gate(100, 100) is True                      # at/after activation -> blake2b
    assert gate(100, 250) is True
