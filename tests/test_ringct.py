"""RingCT — shielded stage 3 confidential amounts (ringct.py). Node-free adversarial crypto tests.

These prove the stage-3 guarantees in isolation (an amount-privacy bug is silent inflation, so the crypto
is tested BEFORE any consensus wiring): Pedersen commitments, the bit range proof, the commitment balance,
and the 2-column MLSAG that hides which input is spent while proving its hidden amount conserves. The
headline RingCT win is exercised directly: a ring may mix notes of DIFFERENT amounts (no stage-2
denomination rule), and every inflation/tamper/forge/replay path is rejected.

Run with: python3 -m pytest tests/test_ringct.py -v
"""
import copy

import pytest

import amounts
import ringct as rct
import shieldedv1 as sh


def _sidecar(n):
    """An on-chain note dict -> the sidecar-row shape verify_spend reads (p_pub/commitment/note_id)."""
    return {"p_pub": n["P"], "commitment": n["C"], "note_id": n["note_id"]}


def _wallet_with_notes(amounts_bis):
    w = sh.new_keypair()
    notes = [rct.make_output(w["address"], str(v)) for v in amounts_bis]
    return w, notes


# ----------------------------------------------------------------------------- primitives
def test_pedersen_homomorphic_and_zero():
    a = rct.commit(1234, 0x55aa)
    b = rct.commit(9999, 0x1234)
    assert rct._add(a, b).format() == rct.commit(1234 + 9999, 0x55aa + 0x1234).format()
    # zero amount is handled (no 0*H crash); commit(0,b) == b*G
    assert rct.commit(0, 7).format() == rct._Gx(7).format()


def test_outputs_carry_one_aggregated_bulletproof():
    # the spend carries a SINGLE aggregated Bulletproof over all outputs (not a per-output proof), and the
    # outputs no longer carry an 'rp' field — range-proof adversarial coverage lives in test_bulletproof.py
    w, notes = _wallet_with_notes([20, 5])
    p_hex, amt, blind, _ = rct.scan_output(notes[0], w["a"], w["b"])
    r2, r3 = sh.new_keypair(), sh.new_keypair()
    spend = rct.make_spend(notes, 0, p_hex, amt, blind,
                           [rct.output_for_spend(r2["address"], "12"), rct.output_for_spend(r3["address"], "8")])
    assert "bp" in spend and all("rp" not in o for o in spend["out"]), "one aggregated proof, no per-output rp"
    assert rct.verify_spend([_sidecar(n) for n in notes], spend)


def test_tampered_rangeproof_rejected():
    # corrupting the aggregated range proof (or dropping it) must make verify_spend fail
    w, notes = _wallet_with_notes([20, 5])
    p_hex, amt, blind, _ = rct.scan_output(notes[0], w["a"], w["b"])
    spend = rct.make_spend(notes, 0, p_hex, amt, blind, [rct.output_for_spend(sh.new_keypair()["address"], "20")])
    t = copy.deepcopy(spend)
    t["bp"]["t"] = "%064x" % ((int(t["bp"]["t"], 16) + 1) % rct._N)
    with pytest.raises(ValueError):
        rct.verify_spend([_sidecar(n) for n in notes], t)
    t2 = copy.deepcopy(spend); t2["bp"] = {}
    with pytest.raises(ValueError):
        rct.verify_spend([_sidecar(n) for n in notes], t2)


# ----------------------------------------------------------------------------- notes
def test_mint_scan_roundtrip_and_privacy():
    w, notes = _wallet_with_notes([20])
    got = rct.scan_output(notes[0], w["a"], w["b"])
    assert got and got[1] == amounts.to_units("20") and got[3] == "bis"
    # the on-chain note exposes only a commitment, never the amount
    assert "amt" in notes[0] and notes[0]["amt"] == amounts.to_units("20")   # PUBLIC only at mint (deposit)
    stranger = sh.new_keypair()
    assert rct.scan_output(notes[0], stranger["a"], stranger["b"]) is None


# ----------------------------------------------------------------------------- confidential spend
def test_confidential_spend_mixes_amounts_and_verifies():
    """The RingCT win: a ring may mix DIFFERENT amounts (no stage-2 denomination rule)."""
    w, notes = _wallet_with_notes([20, 20, 5])           # mixed amounts in one ring
    p_hex, amt, blind, _ = rct.scan_output(notes[0], w["a"], w["b"])
    r2, r3 = sh.new_keypair(), sh.new_keypair()
    outs = [rct.output_for_spend(r2["address"], "12"), rct.output_for_spend(r3["address"], "8")]
    spend = rct.make_spend(notes, 0, p_hex, amt, blind, outs)
    assert spend["v"] == 3 and len(spend["ring"]) == 3
    img = rct.verify_spend([_sidecar(n) for n in notes], spend)
    assert img == rct._ppt(spend["I"]).format().hex()
    # output recipients open their hidden amounts
    k2 = rct.scan_output(spend["out"][0], r2["a"], r2["b"])
    assert k2 and k2[1] == amounts.to_units("12")


def test_inflation_unbalanced_rejected():
    w, notes = _wallet_with_notes([20, 5])
    p_hex, amt, blind, _ = rct.scan_output(notes[0], w["a"], w["b"])
    r2, r3 = sh.new_keypair(), sh.new_keypair()
    # outputs 12 + 9 = 21 != 20 -> creates value from nothing -> balance check must reject
    bad = rct.make_spend(notes, 0, p_hex, amt, blind,
                         [rct.output_for_spend(r2["address"], "12"), rct.output_for_spend(r3["address"], "9")])
    with pytest.raises(ValueError, match="balance"):
        rct.verify_spend([_sidecar(n) for n in notes], bad)


def test_output_tamper_rejected():
    w, notes = _wallet_with_notes([20, 5])
    p_hex, amt, blind, _ = rct.scan_output(notes[0], w["a"], w["b"])
    r2 = sh.new_keypair()
    spend = rct.make_spend(notes, 0, p_hex, amt, blind, [rct.output_for_spend(r2["address"], "20")])
    t = copy.deepcopy(spend)
    t["out"][0]["C"] = notes[1]["C"]                     # swap the output commitment post-signing
    with pytest.raises(ValueError):
        rct.verify_spend([_sidecar(n) for n in notes], t)


def test_double_spend_key_image_links():
    w, notes = _wallet_with_notes([20, 20, 5])
    p_hex, amt, blind, _ = rct.scan_output(notes[0], w["a"], w["b"])
    a = rct.make_spend(notes, 0, p_hex, amt, blind, [rct.output_for_spend(sh.new_keypair()["address"], "20")])
    # re-spend the SAME note via a different ring/outputs -> identical key image (consensus rejects replay)
    b = rct.make_spend([notes[0], notes[2]], 0, p_hex, amt, blind,
                       [rct.output_for_spend(sh.new_keypair()["address"], "20")])
    assert a["I"] == b["I"]


def test_wrong_message_and_outsider_forgery_rejected():
    w, notes = _wallet_with_notes([20, 5])
    p_hex, amt, blind, _ = rct.scan_output(notes[0], w["a"], w["b"])
    spend = rct.make_spend(notes, 0, p_hex, amt, blind,
                           [rct.output_for_spend(sh.new_keypair()["address"], "20")])
    # if consensus resolves the ring in a DIFFERENT order than was signed, the MLSAG message changes ->
    # reject (the signature binds the exact ring membership + order)
    with pytest.raises(ValueError):
        rct.verify_spend([_sidecar(n) for n in reversed(notes)], spend)
    # an outsider who does NOT own the ring member cannot produce a spend that verifies: signing with a
    # key that is not P[real]'s discrete log yields an MLSAG that fails verification
    stranger = sh.new_keypair()
    snotes = [rct.make_output(stranger["address"], "20"), rct.make_output(stranger["address"], "20")]
    fake_priv = "%064x" % rct._rnd()                     # a key that opens NOTHING in the ring
    forged = rct.make_spend(snotes, 0, fake_priv, amounts.to_units("20"), rct._rnd(),
                            [rct.output_for_spend(stranger["address"], "20")])
    with pytest.raises(ValueError):
        rct.verify_spend([_sidecar(n) for n in snotes], forged)


def test_spend_bounds_validated():
    w, notes = _wallet_with_notes([20])
    p_hex, amt, blind, _ = rct.scan_output(notes[0], w["a"], w["b"])
    with pytest.raises(ValueError):                      # too many outputs
        rct.make_spend(notes, 0, p_hex, amt, blind,
                       [rct.output_for_spend(sh.new_keypair()["address"], "1")] * (rct.MAX_OUTPUTS + 1))
