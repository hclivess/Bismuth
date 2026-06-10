"""Unit tests for the stage-2 ring signature primitives (doc/22 §12) — pure crypto, no node needed.

Run with: python3 -m pytest tests/test_ring_signature.py -v
"""
import coincurve as cc

import shieldedv1 as sh

_N = sh._N


def _ring(n):
    secrets = [int.from_bytes(__import__("os").urandom(32), "big") % _N for _ in range(n)]
    pubs = [cc.PublicKey.from_valid_secret(sh._sb(x)) for x in secrets]
    return secrets, pubs


def test_valid_signature_verifies():
    secrets, ring = _ring(5)
    I, c, r = sh.ring_sign(b"msg", ring, secrets[2], 2)
    assert sh.ring_verify(b"msg", ring, I, c, r)


def test_tampered_message_fails():
    secrets, ring = _ring(4)
    I, c, r = sh.ring_sign(b"msg", ring, secrets[1], 1)
    assert not sh.ring_verify(b"other", ring, I, c, r)


def test_tampered_response_fails():
    secrets, ring = _ring(4)
    I, c, r = sh.ring_sign(b"msg", ring, secrets[0], 0)
    r2 = list(r); r2[0] = (r2[0] + 1) % _N
    assert not sh.ring_verify(b"msg", ring, I, c, r2)


def test_swapped_key_image_fails():
    secrets, ring = _ring(4)
    I, c, r = sh.ring_sign(b"msg", ring, secrets[0], 0)
    I_other, _, _ = sh.ring_sign(b"msg", ring, secrets[1], 1)
    assert not sh.ring_verify(b"msg", ring, I_other, c, r)


def test_key_image_links_same_signer_and_differs_across_signers():
    secrets, ring = _ring(6)
    # the key image is deterministic in the spent key (links a double-spend) ...
    I_a = sh.key_image(secrets[3], ring[3])
    I_b = sh.key_image(secrets[3], ring[3])
    assert I_a.format() == I_b.format()
    # ... but a different real signer yields a different image (no false link)
    I_c = sh.key_image(secrets[4], ring[4])
    assert I_a.format() != I_c.format()


def test_outsider_cannot_forge_membership():
    secrets, ring = _ring(4)
    outsider = int.from_bytes(__import__("os").urandom(32), "big") % _N
    # the outsider's key is NOT ring[2]; signing as if it were produces a sig that must not verify
    I, c, r = sh.ring_sign(b"msg", ring, outsider, 2)
    assert not sh.ring_verify(b"msg", ring, I, c, r)


def test_ring_size_one_is_valid_but_offers_no_anonymity():
    secrets, ring = _ring(1)
    I, c, r = sh.ring_sign(b"msg", ring, secrets[0], 0)
    assert sh.ring_verify(b"msg", ring, I, c, r)


def test_hash_to_point_is_on_curve_and_varies():
    p1 = sh.hash_to_point(b"a")
    p2 = sh.hash_to_point(b"b")
    assert isinstance(p1, cc.PublicKey) and isinstance(p2, cc.PublicKey)
    assert p1.format() != p2.format()
    assert sh.hash_to_point(b"a").format() == p1.format()   # deterministic
