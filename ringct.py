"""Shielded value — stage 3: RingCT (CONFIDENTIAL AMOUNTS). See doc/22 §13.

Stage 1 gave recipient privacy (stealth one-time keys), stage 2 gave sender privacy (LSAG ring over
same-amount notes). Stage 3 HIDES THE AMOUNTS: a note carries a Pedersen commitment `C = a·H + b·G`
instead of a cleartext amount, and a spend proves — without revealing `a` — that (i) every output amount
is in range (no negative/overflow inflation), (ii) inputs and outputs balance, and (iii) the spender owns
one ring member, all while hiding WHICH one. Because amounts are hidden, the stage-2 same-amount
denomination rule disappears: a ring may mix notes of any amount.

Built on the secp256k1 helpers in ``shieldedv1`` (libsecp256k1 via coincurve) — no new crypto dependency.
Every primitive here was validated adversarially in isolation before consensus wiring (an amount-privacy
bug is silent inflation). The pieces:

  * Pedersen commitment ``C = a·H + b·G``; ``H`` is a NUMS point ``hash_to_point(G)`` (unknown dlog wrt G).
  * Range proof: bit decomposition + a Schnorr OR-proof per bit that ``C_i`` commits to 0 or 2^i; the bit
    commitments must sum to ``C``. Proves ``0 <= a < 2^RANGE_BITS`` (Monero pre-Bulletproof construction).
  * Balance: a spender publishes a PSEUDO-OUTPUT commitment ``C'`` to the spent amount with blinding equal
    to the sum of the output blindings, so ``C' == ΣC_out`` is a plain point equality (== balance).
  * MLSAG (2 columns): column 0 = the one-time key ``P`` (+ key image for double-spend); column 1 = the
    offset ``C_real − C'`` which, IFF the real input and the pseudo-output commit to the SAME amount, is
    ``z·G`` (a commitment to zero) whose dlog the spender knows. Signing both columns at the hidden real
    index proves ownership AND amount-match together, hiding the index.

secp256k1/coincurve has NO point at infinity and rejects the scalar 0, so the zero coefficient is
special-cased, all random scalars are forced nonzero, and every verifier fails CLOSED on any exception.
"""
import json
import os
from base64 import b64decode, b64encode
from hashlib import sha256

import coincurve as cc

import amounts
import bulletproof as bp
import shieldedv1 as sh

__version__ = "0.3.0"

_N = sh._N
RANGE_BITS = 64                 # output amounts proven in [0, 2^64) — covers any real BIS amount in units
MAX_OUTPUTS = 4                 # bound the openfield: each range proof is ~20 KB hex
MAX_RING = sh.MAX_RING

# NUMS value generator H: a curve point with no known discrete log relative to G.
_G = sh._g_mul(1)
H = sh.hash_to_point(b"bis-ringct-H/v1|" + _G.format())


# --- scalar / point helpers (delegate to the libsecp256k1-backed shieldedv1) ----------------------
def _rnd():
    while True:
        x = int.from_bytes(os.urandom(32), "big") % _N
        if x:
            return x


def _Gx(s):
    return sh._g_mul(s % _N)


def _mul(P, s):
    return sh._point_mul(P, s % _N)          # caller guarantees s % N != 0


def _add(*pts):
    return sh._point_add(*pts)


def _neg(P):
    return sh._point_mul(P, _N - 1)


def _Hp(P):
    return sh.hash_to_point(P.format())


def _phex(P):
    return P.format().hex()


def _ppt(hexstr):
    return cc.PublicKey(bytes.fromhex(hexstr))


def _shex(x):
    return (x % _N).to_bytes(32, "big").hex()


def commit(amount, blind):
    """Pedersen commitment C = amount·H + blind·G (blind must be nonzero; amount 0 is handled)."""
    a = int(amount) % _N
    b = int(blind) % _N
    return _Gx(b) if a == 0 else _add(_mul(H, a), _Gx(b))


# Range proofs live in bulletproof.py (logarithmic, aggregated, batch-verifiable) — see doc/22 §13. The
# earlier O(n) bit-decomposition proof was superseded by it and removed; ``bp.prove``/``bp.verify`` are the
# single range-proof system, exercised on every confidential output below.


# --- MLSAG (2-column linkable ring: key + commitment offset) --------------------------------------
def _mlsag_challenge(msg, items):
    d = sha256(b"bis-ringct-mlsag/v1|")
    d.update(msg)
    for p in items:
        d.update(p.format())
    return int.from_bytes(d.digest(), "big") % _N


def mlsag_sign(msg, ring_P, ring_D, real, x, z):
    """Sign `msg` proving, at the hidden index `real`, knowledge of x (ring_P[real]=x·G) AND z
    (ring_D[real]=z·G). Returns (key_image, c[], r0[], r1[]). Raises only on degenerate RNG (re-draws)."""
    n = len(ring_P)
    I = _mul(_Hp(ring_P[real]), x)
    c = [0] * n
    r0 = [0] * n
    r1 = [0] * n
    L0 = [None] * n
    R0 = [None] * n
    L1 = [None] * n
    k0, k1 = _rnd(), _rnd()
    L0[real] = _Gx(k0)
    R0[real] = _mul(_Hp(ring_P[real]), k0)
    L1[real] = _Gx(k1)
    tot = 0
    for i in range(n):
        if i == real:
            continue
        c[i], r0[i], r1[i] = _rnd(), _rnd(), _rnd()
        L0[i] = _add(_Gx(r0[i]), _mul(ring_P[i], c[i]))
        R0[i] = _add(_mul(_Hp(ring_P[i]), r0[i]), _mul(I, c[i]))
        L1[i] = _add(_Gx(r1[i]), _mul(ring_D[i], c[i]))
        tot = (tot + c[i]) % _N
    items = []
    for i in range(n):
        items += [L0[i], R0[i], L1[i]]
    ch = _mlsag_challenge(msg, items)
    c[real] = (ch - tot) % _N
    r0[real] = (k0 - c[real] * x) % _N
    r1[real] = (k1 - c[real] * z) % _N
    if 0 in (c[real], r0[real], r1[real]):
        return mlsag_sign(msg, ring_P, ring_D, real, x, z)
    return I, c, r0, r1


def mlsag_verify(msg, ring_P, ring_D, I, c, r0, r1):
    try:
        n = len(ring_P)
        if not (n == len(ring_D) == len(c) == len(r0) == len(r1)):
            return False
        items = []
        for i in range(n):
            L0 = _add(_Gx(r0[i]), _mul(ring_P[i], c[i]))
            R0 = _add(_mul(_Hp(ring_P[i]), r0[i]), _mul(I, c[i]))
            L1 = _add(_Gx(r1[i]), _mul(ring_D[i], c[i]))
            items += [L0, R0, L1]
        return (sum(c) % _N) == _mlsag_challenge(msg, items)
    except Exception:
        return False


# --- confidential notes (stealth address + hidden amount) -----------------------------------------
def _enc_amount(ss, amount, blind, token):
    return sh._encrypt_memo(ss, {"amt": int(amount), "blind": _shex(blind), "tok": token})


def make_output(address, amount, token="bis"):
    """A MINT output: move a transparent deposit into the shielded pool as a confidential note. The amount
    equals the on-chain BIS deposit (public at the shielding boundary, like Zcash t->z), so the commitment
    blind is published too and NO range proof is needed (a public amount can't be negative/overflow). The
    note is then spendable CONFIDENTIALLY. `amount` is BIS (str/num) -> integer units."""
    A_bytes, B_bytes = sh.parse_address(address)
    r = cc.PrivateKey()
    R = r.public_key.format()
    ss = sh._shared_secret_sender(r, A_bytes)
    P = sh._one_time_pub(B_bytes, ss)
    units = amounts.to_units(amount)
    blind = _rnd()
    C = commit(units, blind)
    return {
        "v": 3, "R": R.hex(), "P": P.hex(), "C": _phex(C), "tok": token,
        "amt": units, "blind": _shex(blind),          # PUBLIC at mint (== transparent deposit)
        "memo": _enc_amount(ss, units, blind, token),
        "note_id": sh.note_id(P),
    }


def scan_output(note, a_hex, b_hex):
    """Is this confidential note ours? Returns (one_time_priv_hex, amount_units, blind_int, token) or None.
    The recipient needs the blind (from the memo) to later spend, and re-checks commit(amount,blind)==C."""
    try:
        a = cc.PrivateKey(bytes.fromhex(a_hex))
        ss = sh._shared_secret_recipient(a, bytes.fromhex(note["R"]))
        B_bytes = cc.PrivateKey(bytes.fromhex(b_hex)).public_key.format()
        if sh._one_time_pub(B_bytes, ss).hex() != note["P"]:
            return None
        ot = sh._scalar(b"bis-shield-ot/v1", ss)
        p = cc.PrivateKey(bytes.fromhex(b_hex)).add(ot)
        memo = sh._decrypt_memo(ss, note["memo"])
        amt, blind = int(memo["amt"]), int(memo["blind"], 16)
        if commit(amt, blind).format().hex() != note["C"]:
            return None                          # memo/commitment disagree -> not a well-formed note for us
        return p.to_hex(), amt, blind, memo.get("tok", "bis")
    except Exception:
        return None


# --- confidential spend (single input hidden in a ring, confidential outputs) ---------------------
def _spend_message(ring_ids, image_hex, c_pseudo_hex, outputs):
    payload = {"out": [{"P": o["P"], "C": o["C"]} for o in outputs], "Cp": c_pseudo_hex}
    return (b"bis-ringct-spend/v1|" + "|".join(ring_ids).encode() + b"|" + image_hex.encode() + b"|"
            + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def make_spend(ring_notes, real_index, p_hex, in_amount, in_blind, outputs):
    """Spend ring_notes[real_index] (one-time key p_hex, opening (in_amount,in_blind)) hidden in the ring,
    into `outputs` (built by make_output_pinned with blinds summing to a chosen total). Σ out amounts must
    equal in_amount. Returns the spend dict for the chain."""
    if not (1 <= len(ring_notes) <= MAX_RING):
        raise ValueError("ring size out of range")
    if not (1 <= len(outputs) <= MAX_OUTPUTS):
        raise ValueError("output count out of range")
    ring_P = [_ppt(nt["P"]) for nt in ring_notes]
    ring_C = [_ppt(nt["C"]) for nt in ring_notes]
    ring_ids = [nt["note_id"] for nt in ring_notes]
    x = int(p_hex, 16)
    # pseudo-output commits to the spent amount with blinding = Σ output blinds, so C_pseudo == ΣC_out.
    out_blind_sum = 0
    for o in outputs:
        out_blind_sum = (out_blind_sum + int(o["_blind"], 16)) % _N
    C_pseudo = commit(in_amount, out_blind_sum)
    z = (int(in_blind) - out_blind_sum) % _N                  # dlog of (C_real - C_pseudo), a commitment to 0
    ring_D = [_add(ring_C[i], _neg(C_pseudo)) for i in range(len(ring_notes))]
    image = _mul(_Hp(ring_P[real_index]), x)
    image_hex = image.format().hex()
    # ONE aggregated Bulletproof over every output amount (each in [0, 2^64)) — replaces the per-output
    # bit-decomposition proofs (~20 KB each) with a ~1 KB logarithmic proof for the whole spend.
    rangeproof = bp.prove([int(o["_amt"]) for o in outputs], [int(o["_blind"], 16) for o in outputs])
    pub_outputs = [{k: v for k, v in o.items() if k not in ("_blind", "_amt")} for o in outputs]
    msg = _spend_message(ring_ids, image_hex, _phex(C_pseudo), pub_outputs)
    I, c, r0, r1 = mlsag_sign(msg, ring_P, ring_D, real_index, x, z)
    return {
        "v": 3, "ring": ring_ids, "I": image_hex, "Cp": _phex(C_pseudo),
        "c": [_shex(v) for v in c], "r0": [_shex(v) for v in r0], "r1": [_shex(v) for v in r1],
        "out": pub_outputs, "bp": rangeproof,
    }


def output_for_spend(address, amount, token="bis"):
    """Build a confidential spend output: stealth one-time key + commitment + encrypted (amount, blind)
    memo. The range proof is NO LONGER per-output — make_spend aggregates ALL outputs into ONE Bulletproof
    (doc/22 §13). Exposes '_blind'/'_amt' so make_spend can pin Σ blinds (balance) and build that proof;
    both are stripped before publishing."""
    A_bytes, B_bytes = sh.parse_address(address)
    r = cc.PrivateKey()
    R = r.public_key.format()
    ss = sh._shared_secret_sender(r, A_bytes)
    P = sh._one_time_pub(B_bytes, ss)
    units = amounts.to_units(amount)
    blind = _rnd()
    C = commit(units, blind)
    return {
        "v": 3, "R": R.hex(), "P": P.hex(), "C": _phex(C), "tok": token,
        "memo": _enc_amount(ss, units, blind, token), "note_id": sh.note_id(P),
        "_blind": _shex(blind), "_amt": int(units),
    }


def _redeem_message(ring_ids, image_hex, c_pseudo_hex, to_address, amount):
    payload = {"to": to_address, "amt": int(amount), "Cp": c_pseudo_hex}
    return (b"bis-ringct-redeem/v1|" + "|".join(ring_ids).encode() + b"|" + image_hex.encode() + b"|"
            + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def make_redeem(ring_notes, real_index, p_hex, in_amount, in_blind, to_address):
    """Redeem the confidential note ring_notes[real_index] (opening (in_amount,in_blind)) to a TRANSPARENT
    `to_address` for the whole `in_amount`. The amount is REVEALED here (the shielded->transparent boundary)
    along with the pseudo blind, so consensus can check commit(in_amount,blind)==C_pseudo; the MLSAG proves
    the hidden real input commits to exactly that amount."""
    if not (1 <= len(ring_notes) <= MAX_RING):
        raise ValueError("ring size out of range")
    ring_P = [_ppt(nt["P"]) for nt in ring_notes]
    ring_C = [_ppt(nt["C"]) for nt in ring_notes]
    ring_ids = [nt["note_id"] for nt in ring_notes]
    x = int(p_hex, 16)
    bp = _rnd()
    C_pseudo = commit(in_amount, bp)
    z = (int(in_blind) - bp) % _N
    ring_D = [_add(ring_C[i], _neg(C_pseudo)) for i in range(len(ring_notes))]
    image_hex = _mul(_Hp(ring_P[real_index]), x).format().hex()
    msg = _redeem_message(ring_ids, image_hex, _phex(C_pseudo), to_address, in_amount)
    I, c, r0, r1 = mlsag_sign(msg, ring_P, ring_D, real_index, x, z)
    return {
        "v": 3, "ring": ring_ids, "I": image_hex, "Cp": _phex(C_pseudo),
        "blind": _shex(bp), "amt": int(in_amount), "to": to_address,
        "c": [_shex(v) for v in c], "r0": [_shex(v) for v in r0], "r1": [_shex(v) for v in r1],
    }


def verify_redeem(ring_notes, redeem):
    """Consensus verification of a v3 confidential redeem. Returns (key_image_hex, amount_units, to_address);
    raises ValueError on failure. The revealed (amount, blind) must open C_pseudo, and the MLSAG must tie the
    hidden real input's commitment to it — so the redeemed amount provably equals the spent note's amount."""
    try:
        amt = int(redeem["amt"])
        pseudo_blind = int(redeem["blind"], 16)   # NB: not 'bp' — that's the bulletproof module here
        to = redeem["to"]
        C_pseudo = _ppt(redeem["Cp"])
        image_hex = cc.PublicKey(bytes.fromhex(redeem["I"])).format().hex()
        c = [int(v, 16) for v in redeem["c"]]
        r0 = [int(v, 16) for v in redeem["r0"]]
        r1 = [int(v, 16) for v in redeem["r1"]]
        ring_P = [_ppt(nt["p_pub"]) for nt in ring_notes]
        ring_C = [_ppt(nt["commitment"]) for nt in ring_notes]
    except Exception:
        raise ValueError("malformed redeem")
    if amt <= 0:
        raise ValueError("redeem amount must be positive")
    if commit(amt, pseudo_blind).format() != C_pseudo.format():
        raise ValueError("revealed (amount, blind) does not open C_pseudo")
    ring_D = [_add(ring_C[i], _neg(C_pseudo)) for i in range(len(ring_notes))]
    ring_ids = [nt["note_id"] for nt in ring_notes]
    msg = _redeem_message(ring_ids, image_hex, _phex(C_pseudo), to, amt)
    if not mlsag_verify(msg, ring_P, ring_D, _ppt(image_hex), c, r0, r1):
        raise ValueError("redeem MLSAG verification failed")
    return image_hex, amt, to


def verify_spend(ring_notes, spend):
    """Consensus verification of a v3 confidential spend. Returns the canonical key-image hex on success;
    raises ValueError on ANY failure. Checks: MLSAG (ownership + amount-match, hidden index), the aggregated
    Bulletproof range proof over all outputs, and the balance C_pseudo == Σ C_out. Caller checks key-image
    freshness."""
    try:
        ring_P = [_ppt(nt["p_pub"]) for nt in ring_notes]
        ring_C = [_ppt(nt["commitment"]) for nt in ring_notes]
    except Exception:
        raise ValueError("ring member missing P/commitment")
    outs = spend.get("out") or []
    if not (1 <= len(outs) <= MAX_OUTPUTS):
        raise ValueError("bad output count")
    try:
        C_pseudo = _ppt(spend["Cp"])
        out_Cs = [_ppt(o["C"]) for o in outs]
        image_hex = cc.PublicKey(bytes.fromhex(spend["I"])).format().hex()   # canonicalize
        c = [int(v, 16) for v in spend["c"]]
        r0 = [int(v, 16) for v in spend["r0"]]
        r1 = [int(v, 16) for v in spend["r1"]]
    except Exception:
        raise ValueError("malformed spend fields")
    # ONE aggregated Bulletproof proves EVERY output amount is in [0, 2^64) (doc/22 §13) — this is the
    # defense against negative/overflow inflation in the hidden amounts, incl. the field-wraparound attack.
    if not bp.verify(out_Cs, spend.get("bp", {})):
        raise ValueError("aggregated range proof failed")
    # balance: C_pseudo must equal the sum of output commitments (value conserved, blindings pinned)
    acc = out_Cs[0]
    for C in out_Cs[1:]:
        acc = _add(acc, C)
    if acc.format() != C_pseudo.format():
        raise ValueError("confidential balance check failed")
    # MLSAG over (P, C - C_pseudo): proves ownership of one input AND that its amount == pseudo amount
    ring_D = [_add(ring_C[i], _neg(C_pseudo)) for i in range(len(ring_notes))]
    ring_ids = [nt["note_id"] for nt in ring_notes]
    msg = _spend_message(ring_ids, image_hex, _phex(C_pseudo), outs)
    if not mlsag_verify(msg, ring_P, ring_D, _ppt(image_hex), c, r0, r1):
        raise ValueError("MLSAG verification failed")
    return image_hex
