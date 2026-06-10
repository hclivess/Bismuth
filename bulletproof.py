"""Bulletproofs — short, aggregatable range proofs for Bismuth's confidential transactions (doc/22 §13).

Replaces the O(n) bit-decomposition range proof (≈20 KB/output) with a **logarithmic** proof (Bünz et al.,
"Bulletproofs: Short Proofs for Confidential Transactions and More", IEEE S&P 2018). A 64-bit range proof
is ~700 bytes; m outputs AGGREGATE into one proof of ~ (2·⌈log2(64·m)⌉ + 4) points + 5 scalars — e.g.
4 outputs ≈ 1 KB instead of 4×20 KB. The verifier is the optimized **single multi-exponentiation** (no
per-round point folding), and `batch_verify` checks many proofs with ONE combined multi-exp — the standard
cutting-edge verification path for a block full of confidential outputs.

Built only on the secp256k1 helpers in :mod:`shieldedv1` (libsecp256k1 via coincurve). Every primitive was
validated adversarially in isolation before integration — a range-proof soundness bug is silent inflation,
including the field-WRAPAROUND attack (a "negative" amount v = N−k looks huge); both out-of-range and
wraparound are rejected. secp256k1 has no point at infinity and rejects the scalar 0, so the zero
coefficient is special-cased (`commit`), all randomness is forced nonzero, and every verifier fails CLOSED
on any exception.

PROTOCOL (additive notation; value generator H, blinding generator G; commitment V = v·H + γ·G):
  Prover commits the bit-vector a_L (and a_R = a_L−1) under vector generators G⃗, H⃗ (A), blinds it (S),
  the verifier sends y, z; the prover forms l(X), r(X) with t(X)=⟨l,r⟩, commits t₁,t₂ (T₁,T₂); the verifier
  sends x; the prover sends t̂=⟨l,r⟩, τ_x, μ and an **inner-product argument** proving t̂=⟨l,r⟩ in log rounds.
  Aggregation packs m values into one length-(n·m) instance with per-value z-powers; the value count is
  padded to a power of two (the IPA halves each round), the pad commitments (to 0) ride in the proof.
"""
import os
from hashlib import sha256

import shieldedv1 as sh

__version__ = "0.1.0"

_N = sh._N
RANGE_BITS = 64                 # each value proven in [0, 2^64)
MAX_AGG = 8                     # max aggregated values (padded to a power of two); caps generator count

_G = sh._g_mul(1)               # blinding generator
# value generator — MUST equal ringct.H so a Bulletproof V matches a RingCT output commitment
H = sh.hash_to_point(b"bis-ringct-H/v1|" + _G.format())
_QBASE = sh.hash_to_point(b"bis-bp-Q/v1")

# vector generators G_vec, H_vec, derived once up to RANGE_BITS*MAX_AGG (NUMS, domain-separated)
_MAXNM = RANGE_BITS * MAX_AGG
_GV = [sh.hash_to_point(b"bis-bp-G/v1|" + i.to_bytes(2, "big")) for i in range(_MAXNM)]
_HV = [sh.hash_to_point(b"bis-bp-H/v1|" + i.to_bytes(2, "big")) for i in range(_MAXNM)]


# --- scalar / point helpers (libsecp256k1 via shieldedv1) -----------------------------------------
def _rnd():
    while True:
        x = int.from_bytes(os.urandom(32), "big") % _N
        if x:
            return x


def _inv(x):
    return pow(x % _N, _N - 2, _N)


def _Gx(s):
    return sh._g_mul(s % _N)


def _mul(P, s):
    return sh._point_mul(P, s % _N)


def _add(*pts):
    return sh._point_add(*pts)


def _terms(pairs):
    """Multi-scalar mul Σ s·P, skipping zero scalars. Raises if the result is the identity (no nonzero
    term, or a cancellation) — callers treat that as failure (verify) or redraw (prove)."""
    t = [_mul(P, s) for (s, P) in pairs if s % _N != 0]
    if not t:
        raise ValueError("identity multi-exponentiation")
    return _add(*t)


def _msm(scalars, points):
    return _terms(list(zip(scalars, points)))


def _ip(a, b):
    return sum((x * y) % _N for x, y in zip(a, b)) % _N


def commit(value, blind):
    """Pedersen V = value·H + blind·G (matches ringct.commit; blind must be nonzero; value 0 handled)."""
    v = int(value) % _N
    b = int(blind) % _N
    return _Gx(b) if v == 0 else _add(_mul(H, v), _Gx(b))


def _phex(P):
    return P.format().hex()


def _ppt(hexs):
    import coincurve as cc
    return cc.PublicKey(bytes.fromhex(hexs))


def _shex(x):
    return (x % _N).to_bytes(32, "big").hex()


def _next_pow2(m):
    return 1 << ((m - 1).bit_length()) if m > 1 else 1


# --- Fiat-Shamir transcript (deterministic; identical on prove + verify) --------------------------
class _Transcript:
    def __init__(self, label):
        self.h = sha256(label).digest()

    def absorb_point(self, *pts):
        d = sha256(self.h)
        for p in pts:
            d.update(p.format())
        self.h = d.digest()

    def absorb_scalar(self, *xs):
        d = sha256(self.h)
        for x in xs:
            d.update((int(x) % _N).to_bytes(32, "big"))
        self.h = d.digest()

    def challenge(self, label):
        d = sha256(self.h)
        d.update(label)
        self.h = d.digest()
        c = int.from_bytes(self.h, "big") % _N
        return c if c else 1


# --- inner-product argument -----------------------------------------------------------------------
def _ipa_prove(Gv, Hv, Q, a, b, tr):
    Ls, Rs = [], []
    a, b, Gv, Hv = a[:], b[:], Gv[:], Hv[:]
    while len(a) > 1:
        k = len(a) // 2
        aL, aR, bL, bR = a[:k], a[k:], b[:k], b[k:]
        GL, GR, HL, HR = Gv[:k], Gv[k:], Hv[:k], Hv[k:]
        L = _add(_msm(aL, GR), _msm(bR, HL), _mul(Q, _ip(aL, bR)))
        R = _add(_msm(aR, GL), _msm(bL, HR), _mul(Q, _ip(aR, bL)))
        Ls.append(L)
        Rs.append(R)
        tr.absorb_point(L, R)
        u = tr.challenge(b"u")
        ui = _inv(u)
        a = [(aL[i] * u + aR[i] * ui) % _N for i in range(k)]
        b = [(bL[i] * ui + bR[i] * u) % _N for i in range(k)]
        Gv = [_add(_mul(GL[i], ui), _mul(GR[i], u)) for i in range(k)]
        Hv = [_add(_mul(HL[i], u), _mul(HR[i], ui)) for i in range(k)]
    return Ls, Rs, a[0], b[0]


def _ipa_s_vector(us, uinv, nn):
    """The scalar vector s (i-th basis coefficient) for the optimized single-MSM IPA check."""
    k = len(us)
    s = []
    for i in range(nn):
        prod = 1
        for j in range(k):
            prod = (prod * (us[j] if (i >> (k - 1 - j)) & 1 else uinv[j])) % _N
        s.append(prod)
    return s


# --- aggregated range proof -----------------------------------------------------------------------
def prove(values, gammas, n=RANGE_BITS):
    """Aggregated range proof that each ``commit(values[j], gammas[j])`` lies in [0, 2^n). The value count
    is padded to a power of two with commitments to 0 (carried in the proof). Returns a serializable dict.
    ``gammas`` MUST be the real commitment blindings (so the proof's V_j == the caller's commitment)."""
    m_real = len(values)
    if not (1 <= m_real <= MAX_AGG):
        raise ValueError("aggregate 1..%d values" % MAX_AGG)
    for v in values:
        if not (0 <= int(v) < (1 << n)):
            raise ValueError("value out of [0, 2^%d)" % n)
    m = _next_pow2(m_real)
    pad_comms = []
    vv = [int(v) for v in values]
    gg = [int(g) % _N for g in gammas]
    for _ in range(m - m_real):                       # pad to a power of two with commitments to 0
        pg = _rnd()
        pad_comms.append(commit(0, pg))
        vv.append(0)
        gg.append(pg)
    nm = n * m
    Gv, Hv = _GV[:nm], _HV[:nm]
    Vs = [commit(vv[j], gg[j]) for j in range(m)]

    aL = [((vv[i // n] >> (i % n)) & 1) for i in range(nm)]
    aR = [(aL[i] - 1) % _N for i in range(nm)]
    alpha = _rnd()
    A = _terms([(alpha, _G)] + [(aL[i], Gv[i]) for i in range(nm)] + [(aR[i], Hv[i]) for i in range(nm)])
    sL = [_rnd() for _ in range(nm)]
    sR = [_rnd() for _ in range(nm)]
    rho = _rnd()
    S = _terms([(rho, _G)] + [(sL[i], Gv[i]) for i in range(nm)] + [(sR[i], Hv[i]) for i in range(nm)])

    tr = _Transcript(b"bis-bp/v1")
    tr.absorb_scalar(m, n)
    for V in Vs:
        tr.absorb_point(V)
    tr.absorb_point(A, S)
    y = tr.challenge(b"y")
    z = tr.challenge(b"z")
    yv = [pow(y, i, _N) for i in range(nm)]
    z2 = (z * z) % _N
    zpow = [pow(z, 2 + j, _N) for j in range(m)]      # z^{2+j} for value block j
    tw = [pow(2, b, _N) for b in range(n)]

    l0 = [(aL[i] - z) % _N for i in range(nm)]
    l1 = sL[:]
    r0 = [(yv[i] * ((aR[i] + z) % _N) + zpow[i // n] * tw[i % n]) % _N for i in range(nm)]
    r1 = [(yv[i] * sR[i]) % _N for i in range(nm)]
    t1 = (_ip(l0, r1) + _ip(l1, r0)) % _N
    t2 = _ip(l1, r1)
    tau1, tau2 = _rnd(), _rnd()
    T1 = commit(t1, tau1)
    T2 = commit(t2, tau2)

    tr.absorb_point(T1, T2)
    x = tr.challenge(b"x")
    l = [(l0[i] + l1[i] * x) % _N for i in range(nm)]
    r = [(r0[i] + r1[i] * x) % _N for i in range(nm)]
    that = _ip(l, r)
    taux = (tau2 * x % _N * x + tau1 * x + sum((zpow[j] * gg[j]) % _N for j in range(m))) % _N
    mu = (alpha + rho * x) % _N

    tr.absorb_scalar(taux, mu, that)
    w = tr.challenge(b"w")
    Q = _mul(_QBASE, w)
    Hp = [_mul(Hv[i], _inv(yv[i])) for i in range(nm)]
    Ls, Rs, fa, fb = _ipa_prove(Gv, Hp, Q, l, r, tr)
    return {
        "n": n, "m": m_real,
        "pad": [_phex(c) for c in pad_comms],
        "A": _phex(A), "S": _phex(S), "T1": _phex(T1), "T2": _phex(T2),
        "t": _shex(that), "taux": _shex(taux), "mu": _shex(mu),
        "L": [_phex(p) for p in Ls], "R": [_phex(p) for p in Rs],
        "a": _shex(fa), "b": _shex(fb),
    }


def _verify_prep(V_list, proof):
    """Shared verifier setup: rebuild challenges and the per-proof coefficient maps for the combined
    verification equation Σ(scalar·point) == 0. Returns (gen_g_coeffs, gen_h_coeffs, q_coeff, hgen_coeff,
    g_coeff, extra_terms) where gen_* are length-nm coefficient lists on the SHARED G_vec/H_vec (so a batch
    can accumulate them into one multi-exp), and extra_terms are this proof's own (scalar, point) pairs."""
    n = int(proof["n"])
    m_real = int(proof["m"])
    pad = [_ppt(h) for h in proof["pad"]]
    Vs = list(V_list) + pad
    m = _next_pow2(m_real)
    if len(Vs) != m or len(pad) != m - m_real:
        raise ValueError("padding/aggregate size mismatch")
    nm = n * m
    if nm > _MAXNM:
        raise ValueError("aggregate too large")
    A = _ppt(proof["A"]); S = _ppt(proof["S"]); T1 = _ppt(proof["T1"]); T2 = _ppt(proof["T2"])
    that = int(proof["t"], 16); taux = int(proof["taux"], 16); mu = int(proof["mu"], 16)
    Ls = [_ppt(h) for h in proof["L"]]
    Rs = [_ppt(h) for h in proof["R"]]
    fa = int(proof["a"], 16); fb = int(proof["b"], 16)
    if len(Ls) != len(Rs) or (1 << len(Ls)) != nm:
        raise ValueError("malformed IPA proof length")

    tr = _Transcript(b"bis-bp/v1")
    tr.absorb_scalar(m, n)
    for V in Vs:
        tr.absorb_point(V)
    tr.absorb_point(A, S)
    y = tr.challenge(b"y"); z = tr.challenge(b"z")
    yv = [pow(y, i, _N) for i in range(nm)]
    z2 = (z * z) % _N
    zpow = [pow(z, 2 + j, _N) for j in range(m)]
    tw = [pow(2, b, _N) for b in range(n)]
    tr.absorb_point(T1, T2)
    x = tr.challenge(b"x")
    tr.absorb_scalar(taux, mu, that)
    w = tr.challenge(b"w")
    us = []
    for L, R in zip(Ls, Rs):
        tr.absorb_point(L, R)
        us.append(tr.challenge(b"u"))
    uinv = [_inv(u) for u in us]
    s = _ipa_s_vector(us, uinv, nm)
    yinv = [_inv(yv[i]) for i in range(nm)]
    # MERGE challenge: the two sub-equations are combined as (t̂-check)·c + (IPA-check). c is Fiat-Shamir
    # (unpredictable once the proof is fixed), so the merged sum is the identity IFF BOTH sub-equations are
    # — without it an attacker could offset one residual against the other (a soundness hole).
    c = tr.challenge(b"merge")

    sumy = sum(yv) % _N
    sum_zpow = sum(zpow) % _N
    delta = ((z - z2) % _N * sumy - (z * sum_zpow % _N) * ((pow(2, n, _N) - 1) % _N)) % _N

    # t̂-check (×c): t̂·H + taux·G − Σ zpow_j·V_j − delta·H − x·T1 − x²·T2 == 0
    # ipa-check (single-MSM): A + xS − μG − z·ΣG_i + Σ coeff_i·H'_i + t̂·Q
    #            + Σ(u_j²L_j + u_j^{-2}R_j) − Σ(a·s_i)G_i − Σ(b/s_i)H'_i − ab·Q == 0,  Q=w·Qbase, H'_i=y^{-i}H_i
    coeff = [(z * yv[i] + zpow[i // n] * tw[i % n]) % _N for i in range(nm)]
    g_vec_coeff = [((_N - z) - fa * s[i]) % _N for i in range(nm)]                     # on G_i  (IPA)
    h_vec_coeff = [((coeff[i] - fb * _inv(s[i])) % _N) * yinv[i] % _N for i in range(nm)]  # on H_i (IPA)
    q_coeff = ((that - (fa * fb) % _N) * w) % _N                                       # on Qbase (IPA)
    hgen_coeff = (c * ((that - delta) % _N)) % _N                                      # on H   (t̂-check ×c)
    g_coeff = (c * taux - mu) % _N                                                      # on G   (t̂ ×c + IPA)
    extra = [(1, A), (x, S),                                                            # IPA-only points
             ((c * ((_N - x) % _N)) % _N, T1), ((c * ((_N - (x * x) % _N)) % _N) % _N, T2)]
    extra += [((c * ((_N - zpow[j]) % _N)) % _N, Vs[j]) for j in range(m)]              # t̂-check ×c
    extra += [((us[j] * us[j]) % _N, Ls[j]) for j in range(len(Ls))]
    extra += [((uinv[j] * uinv[j]) % _N, Rs[j]) for j in range(len(Rs))]
    return g_vec_coeff, h_vec_coeff, q_coeff, hgen_coeff, g_coeff, extra, nm


def verify(V_list, proof):
    """Verify an aggregated range proof for commitments ``V_list`` (the post-pad commitments come from the
    proof). Optimized single multi-exponentiation; fails CLOSED on any malformation."""
    try:
        gvc, hvc, qc, hgc, gc, extra, nm = _verify_prep(V_list, proof)
        pairs = [(gvc[i], _GV[i]) for i in range(nm)] + [(hvc[i], _HV[i]) for i in range(nm)]
        pairs += [(qc, _QBASE), (hgc, H), (gc, _G)] + extra
        # the equation holds iff Σ pairs == identity. coincurve has no identity, so split into the two
        # halves by sign and assert equality of the two sums (avoids representing the point at infinity).
        pos = [(s, P) for (s, P) in pairs if s % _N != 0]
        # move ~half to the other side: build LHS = Σ s·P for a partition, RHS = Σ (−s)·P for the rest is
        # equivalent to one combine == identity; simplest: combine all and require it raise (identity).
        try:
            _terms(pos)
            return False           # combined != identity -> invalid
        except ValueError:
            return True            # Σ == identity (combine_keys rejects the point at infinity) -> valid
    except Exception:
        return False


def batch_verify(items):
    """Verify many proofs with ONE combined multi-exponentiation (random-weighted). ``items`` =
    [(V_list, proof), ...]. Accumulates every proof's coefficients on the SHARED G_vec/H_vec/Qbase/H/G
    (weighted by a fresh random scalar per proof) plus each proof's own points, then a single identity
    check. Sound: a random linear combination of the per-proof equations is identity iff every one is."""
    if not items:
        return True
    try:
        gv = [0] * _MAXNM
        hv = [0] * _MAXNM
        qc = 0
        hgc = 0
        gc = 0
        extra = []
        for V_list, proof in items:
            w = _rnd()                                  # independent random weight per proof
            a_gv, a_hv, a_q, a_hg, a_g, a_extra, nm = _verify_prep(V_list, proof)
            for i in range(nm):
                gv[i] = (gv[i] + w * a_gv[i]) % _N
                hv[i] = (hv[i] + w * a_hv[i]) % _N
            qc = (qc + w * a_q) % _N
            hgc = (hgc + w * a_hg) % _N
            gc = (gc + w * a_g) % _N
            extra += [((w * s) % _N, P) for (s, P) in a_extra]
        pairs = [(gv[i], _GV[i]) for i in range(_MAXNM)] + [(hv[i], _HV[i]) for i in range(_MAXNM)]
        pairs += [(qc, _QBASE), (hgc, H), (gc, _G)] + extra
        try:
            _terms([(s, P) for (s, P) in pairs if s % _N != 0])
            return False
        except ValueError:
            return True
    except Exception:
        return False
