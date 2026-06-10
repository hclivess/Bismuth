"""Bulletproofs (bulletproof.py) — aggregated logarithmic range proofs. Node-free adversarial crypto tests.

A range-proof soundness bug is silent inflation, so these are exhaustive: correctness across aggregate
sizes (with power-of-two padding), the logarithmic size, every boundary value, and — the part that matters —
that NO out-of-range / wraparound / tampered / malformed proof verifies, individually OR inside a batch.

Run with: python3 -m pytest tests/test_bulletproof.py -v
"""
import copy
import json

import pytest

import bulletproof as bp
from bulletproof import commit, prove, verify, batch_verify, _rnd, _N, RANGE_BITS

MAXV = (1 << RANGE_BITS) - 1


def _V(v, g):
    return commit(v, g)


def _mk(vals):
    gs = [_rnd() for _ in vals]
    return [_V(vals[i], gs[i]) for i in range(len(vals))], prove(vals, gs)


# ----------------------------------------------------------------------------- correctness
@pytest.mark.parametrize("m", [1, 2, 3, 4, 5, 8])
def test_aggregate_sizes_verify(m):
    vals = [_rnd() % (1 << 64) for _ in range(m)]
    Vs, pf = _mk(vals)
    assert verify(Vs, pf)
    # padding to a power of two is reflected in the proof
    mp = 1 << ((m - 1).bit_length()) if m > 1 else 1
    assert len(pf["pad"]) == mp - m


def test_boundary_values():
    for vals in ([0], [MAXV], [0, MAXV], [MAXV, 0, 1, MAXV]):
        Vs, pf = _mk(vals)
        assert verify(Vs, pf), vals


def test_proof_is_logarithmic():
    # a 4-value (256-bit) aggregate is a handful of points, NOT O(n) — the whole point of Bulletproofs
    _, pf = _mk([_rnd() % (1 << 64) for _ in range(4)])
    assert len(pf["L"]) == len(pf["R"]) == 8        # log2(64*4) = 8 rounds
    assert len(json.dumps(pf)) < 3000, "aggregated proof must be ~KB, not tens of KB"


def test_prove_rejects_out_of_range_input():
    with pytest.raises(ValueError):
        prove([1 << RANGE_BITS], [_rnd()])
    with pytest.raises(ValueError):
        prove([-1], [_rnd()])
    with pytest.raises(ValueError):
        prove([_rnd() for _ in range(bp.MAX_AGG + 1)], [_rnd() for _ in range(bp.MAX_AGG + 1)])


# ----------------------------------------------------------------------------- soundness
def test_out_of_range_value_rejected():
    # honest proof of the low 64 bits, but the REAL commitment is to value + 2^64 -> reject
    g = _rnd()
    real = (1 << 64) + 12345
    pf = prove([real & MAXV], [g])
    assert not verify([_V(real, g)], pf), "out-of-range commitment must be rejected"


def test_field_wraparound_negative_rejected():
    # v = N-7 is a huge field element ("negative" amount); its low 64 bits prove but the commitment doesn't
    g = _rnd()
    neg = (_N - 7) % _N
    pf = prove([neg & MAXV], [g])
    assert not verify([_V(neg, g)], pf), "field-wraparound negative amount must be rejected"


def test_wrong_commitment_rejected():
    g = _rnd()
    pf = prove([1000], [g])
    assert not verify([_V(1001, g)], pf)          # different value
    assert not verify([_V(1000, _rnd())], pf)     # different blind


def test_one_bad_member_fails_whole_aggregate():
    gs = [_rnd(), _rnd(), _rnd(), _rnd()]
    vals = [5, (1 << 64) + 1, 7, 9]               # member 1 is out of range
    pf = prove([v & MAXV for v in vals], gs)
    assert not verify([_V(vals[j], gs[j]) for j in range(4)], pf)


def test_every_field_tamper_rejected():
    Vs, pf = _mk([42, 7])
    assert verify(Vs, pf)
    # scalar fields
    for k in ("t", "taux", "mu", "a", "b"):
        t = copy.deepcopy(pf)
        t[k] = "%064x" % ((int(t[k], 16) + 1) % _N)
        assert not verify(Vs, t), "tampered scalar %s must reject" % k
    # point fields
    for k in ("A", "S", "T1", "T2"):
        t = copy.deepcopy(pf)
        t[k] = bp._phex(bp._mul(bp._ppt(t[k]), 2))
        assert not verify(Vs, t), "tampered point %s must reject" % k
    # an IPA round point
    t = copy.deepcopy(pf)
    t["L"][0] = bp._phex(bp._mul(bp._ppt(t["L"][0]), 3))
    assert not verify(Vs, t), "tampered L must reject"


def test_bit_width_is_pinned_no_wraparound_bypass():
    """CRITICAL (adversarial-pass find): a verifier that trusted the proof's `n` would accept a 'negative'
    amount v = N−k ≈ 2²⁵⁶ proven with n=256 — bypassing the range proof and, with the RingCT balance check
    (which holds mod N), minting supply from nothing. The verifier MUST pin n to RANGE_BITS."""
    g = _rnd()
    for nbad in (32, 63, 65, 128, 256):
        assert not verify([_V(100, g)], prove([100], [g], n=nbad)), "n=%d must be rejected" % nbad
    neg = (_N - 7) % _N                                # the actual wraparound value, in [0, 2²⁵⁶) for n=256
    assert not verify([_V(neg, g)], prove([neg], [g], n=256)), "wraparound at n=256 must be rejected"
    # the same attack must not slip through a batch either
    good = _mk([42])
    bad = ([_V(neg, g)], prove([neg], [g], n=256))
    assert not batch_verify([good, bad])


def test_tampered_pad_commitment_rejected():
    # the pad commitments ride in the proof and are absorbed into the transcript; tampering one changes the
    # challenges and must invalidate the proof (they cannot be swapped post-hoc)
    Vs, pf = _mk([3, 4, 5])                            # m=3 -> one pad commitment
    assert verify(Vs, pf) and len(pf["pad"]) == 1
    t = copy.deepcopy(pf)
    t["pad"][0] = bp._phex(bp._mul(bp._ppt(t["pad"][0]), 2))
    assert not verify(Vs, t)


def test_malformed_proof_fails_closed():
    Vs, pf = _mk([42])
    for mut in ({}, {"n": 64, "m": 1}, dict(pf, L=pf["L"][:-1]), dict(pf, a="zz"),
                dict(pf, pad=["00" * 33])):
        assert not verify(Vs, mut), "malformed proof must fail closed, not raise"


def test_padding_slots_cannot_hide_value():
    # m=3 pads to 4; the pad commitment is in the proof. Even if a prover puts a real value in the pad slot
    # it gains nothing (the pad isn't a real output here) and the proof still only verifies its own Vs.
    Vs, pf = _mk([3, 4, 5])
    assert verify(Vs, pf) and len(pf["pad"]) == 1
    # presenting fewer/more commitments than the proof's m -> reject
    assert not verify(Vs[:2], pf)
    assert not verify(Vs + [_V(1, _rnd())], pf)


def test_generators_are_independent():
    """Pedersen BINDING requires no known discrete-log relation between H (value), G (blind) and the vector
    generators / Q — they are NUMS hash-to-point outputs with distinct domains, hence all distinct points.
    A relation would let a prover open a commitment to two different values (counterfeiting)."""
    pts = {bp._G.format(), bp.H.format(), bp._QBASE.format()}
    for i in range(0, bp._MAXNM, 37):
        pts.add(bp._GV[i].format())
        pts.add(bp._HV[i].format())
    n = 3 + len(range(0, bp._MAXNM, 37)) * 2
    assert len(pts) == n, "generators must all be distinct NUMS points"
    # H is shared with ringct so a Bulletproof V equals a RingCT output commitment
    import ringct
    assert bp.H.format() == ringct.H.format()


def test_frozen_heart_value_commitment_is_bound():
    """Fiat-Shamir completeness (the 2022 'Frozen Heart' class): the value commitments V MUST be absorbed
    into the transcript BEFORE the challenges, or a prover could choose V after seeing them and forge. A
    proof made for one V must not verify for any other V."""
    g = _rnd()
    pf = prove([5000], [g])
    assert verify([_V(5000, g)], pf)
    for delta in (1, 7, -3, 1 << 40):
        assert not verify([_V(5000 + delta, g)], pf), "V is not bound — Frozen-Heart-vulnerable"
    assert not verify([_V(5000, _rnd())], pf)        # also bound to the blinding


# ----------------------------------------------------------------------------- batch verification
def test_batch_all_valid_passes():
    items = [_mk([_rnd() % (1 << 64), _rnd() % (1 << 64)]) for _ in range(6)]
    assert batch_verify(items)
    assert batch_verify([])                       # empty batch is vacuously true


def test_batch_one_corrupt_fails():
    items = [_mk([_rnd() % (1 << 64)]) for _ in range(5)]
    bad = list(items)
    Vs, pf = bad[2]
    pf2 = dict(pf)
    pf2["t"] = "%064x" % ((int(pf["t"], 16) + 1) % _N)
    bad[2] = (Vs, pf2)
    assert not batch_verify(bad), "a single corrupt proof must fail the whole batch"


def test_batch_mismatched_commitments_fails():
    items = [_mk([_rnd() % (1 << 64), _rnd() % (1 << 64)]) for _ in range(4)]
    Vs, pf = items[1]
    items[1] = (list(reversed(Vs)), pf)           # swap the commitments the proof was made for
    assert not batch_verify(items)


def test_batch_mixed_sizes():
    # aggregates of different sizes batch together (shared generators accumulate; per-proof points kept)
    items = [_mk([_rnd() % (1 << 64)]),
             _mk([_rnd() % (1 << 64), _rnd() % (1 << 64)]),
             _mk([_rnd() % (1 << 64) for _ in range(4)])]
    assert batch_verify(items)
    items[0] = (items[0][0], dict(items[0][1], a="%064x" % _rnd()))
    assert not batch_verify(items)
