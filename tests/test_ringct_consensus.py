"""RingCT (shielded stage 3) CONSENSUS integration — drives shieldedv1.validate_block/apply_block with v3
ops directly (no node). Proves the confidential path is wired into consensus: mint (public deposit ->
confidential note), confidential spend (mixed-amount ring, hidden amounts, value conserved), redeem
(confidential -> transparent), pool accounting via flows, double-spend rejection, and that v2 and v3
notes coexist in one sidecar.

Run with: python3 -m pytest tests/test_ringct_consensus.py -v
"""
import json
import os
import tempfile

import pytest

import amounts
import ringct as rct
import shieldedv1 as sh


def _state():
    amounts.LEDGER_INTEGER = True
    return sh.ShieldedState(os.path.join(tempfile.mkdtemp(), "shielded-ringct-test.db"))


def _tx(op, openfield, recipient="x" * 56, amount="0"):
    return ("1", "ts", "s" * 56, recipient, amount, "sig", "pub", "bh", "0", "0", op, openfield)


def _mint3(st, wallet, amount_bis, height):
    note = rct.make_output(wallet["address"], str(amount_bis))
    tx = _tx(sh.OP_MINT, json.dumps(note), recipient=sh.SHIELD_SINK, amount=str(note["amt"]))
    sh.apply_block(st, sh.validate_block(st, [tx], height))
    return note


def _spend3(notes, real, w, outs):
    p_hex, amt, blind, _ = rct.scan_output(notes[real], w["a"], w["b"])
    return rct.make_spend(notes, real, p_hex, amt, blind, outs)


def test_v3_mint_spend_redeem_lifecycle():
    st = _state()
    w = sh.new_keypair()
    # mint three MIXED-amount confidential notes (the RingCT win — no denomination rule)
    notes = [_mint3(st, w, v, 1 + i) for i, v in enumerate((20, 13, 5))]
    assert st.pool_units() == amounts.to_units("20") + amounts.to_units("13") + amounts.to_units("5")
    assert all(rct.scan_output(n, w["a"], w["b"]) for n in notes)

    # confidential spend of note[0] (20) hidden in the mixed ring -> 12 + 8
    r2, r3 = sh.new_keypair(), sh.new_keypair()
    outs = [rct.output_for_spend(r2["address"], "12"), rct.output_for_spend(r3["address"], "8")]
    spend = _spend3(notes, 0, w, outs)
    ki0, pool0 = st.stats()["key_images"], st.pool_units()
    parsed = sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10)
    assert parsed and parsed[0][0] == "spend3"
    sh.apply_block(st, parsed)
    assert st.stats()["key_images"] == ki0 + 1, "spend recorded a key image"
    assert st.pool_units() == pool0, "confidential spend is value-neutral (pool unchanged)"
    # the output notes exist (commitments stored, amounts hidden) and the recipient can open them
    assert st.has_note(spend["out"][0]["note_id"]) and st.has_note(spend["out"][1]["note_id"])
    assert rct.scan_output(spend["out"][0], r2["a"], r2["b"])[1] == amounts.to_units("12")

    # redeem note[1] (13) back to a transparent address
    p_hex, amt, blind, _ = rct.scan_output(notes[1], w["a"], w["b"])
    redeem = rct.make_redeem(notes, 1, p_hex, amt, blind, "d" * 56)
    pool1 = st.pool_units()
    parsed = sh.validate_block(st, [_tx(sh.OP_REDEEM, json.dumps(redeem))], 11)
    assert parsed and parsed[0][0] == "redeem3"
    payouts = sh.apply_block(st, parsed)
    assert ("d" * 56, amt) in payouts, "redeem yields a transparent payout"
    assert st.pool_units() == pool1 - amt, "redeem debits the pool by the revealed amount"


def test_v3_double_spend_rejected():
    st = _state()
    w = sh.new_keypair()
    notes = [_mint3(st, w, 9, 1 + i) for i in range(2)]
    spend = _spend3(notes, 0, w, [rct.output_for_spend(sh.new_keypair()["address"], "9")])
    sh.apply_block(st, sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10))
    # re-spend the same note via a different ring/outputs -> same key image -> rejected
    p_hex, amt, blind, _ = rct.scan_output(notes[0], w["a"], w["b"])
    again = rct.make_spend([notes[0], notes[1]], 0, p_hex, amt, blind,
                           [rct.output_for_spend(sh.new_keypair()["address"], "9")])
    assert again["I"] == spend["I"]
    with pytest.raises(sh.ShieldError, match="double-spend"):
        sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(again))], 11)


def test_v3_mint_deposit_must_equal_amount():
    st = _state()
    note = rct.make_output(sh.new_keypair()["address"], "10")
    bad = _tx(sh.OP_MINT, json.dumps(note), recipient=sh.SHIELD_SINK, amount=str(note["amt"] + 1))
    with pytest.raises(sh.ShieldError, match="deposit"):
        sh.validate_block(st, [bad], 1)


def test_v3_mint_commitment_must_open():
    st = _state()
    note = rct.make_output(sh.new_keypair()["address"], "10")
    note["blind"] = "%064x" % ((int(note["blind"], 16) + 1) % rct._N)   # blind no longer opens C
    bad = _tx(sh.OP_MINT, json.dumps(note), recipient=sh.SHIELD_SINK, amount=str(note["amt"]))
    with pytest.raises(sh.ShieldError, match="open"):
        sh.validate_block(st, [bad], 1)


def test_v3_unbalanced_spend_rejected():
    st = _state()
    w = sh.new_keypair()
    notes = [_mint3(st, w, 20, 1 + i) for i in range(2)]
    p_hex, amt, blind, _ = rct.scan_output(notes[0], w["a"], w["b"])
    # 12 + 9 = 21 != 20 -> balance check inside verify_spend fails -> ShieldError
    bad = rct.make_spend(notes, 0, p_hex, amt, blind,
                         [rct.output_for_spend(w["address"], "12"), rct.output_for_spend(w["address"], "9")])
    with pytest.raises(sh.ShieldError, match="invalid"):
        sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(bad))], 10)


def test_v3_rollback_restores_spendability_and_pool():
    st = _state()
    w = sh.new_keypair()
    notes = [_mint3(st, w, 6, 1 + i) for i in range(2)]
    spend = _spend3(notes, 0, w, [rct.output_for_spend(w["address"], "6")])
    sh.apply_block(st, sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10))
    assert st.has_key_image(spend["I"])
    st.rollback_under(10)                       # undo the spend block
    assert not st.has_key_image(spend["I"]), "rollback clears the key image (note spendable again)"


def test_v3_spend_output_junk_amt_rejected_at_validate():
    """Consensus-audit find: a confidential spend output normally has NO amt (it is hidden in C). An
    attacker could attach a junk amt that PASSES validation but throws int(amt) in apply_block — and apply
    runs AFTER to_db under a swallowed except, so the block commits while the sidecar silently desyncs (key
    image burned, outputs dropped). validate_block must reject any non-(non-negative-int) amt up front."""
    st = _state()
    w = sh.new_keypair()
    notes = [_mint3(st, w, v, 1 + i) for i, v in enumerate((20, 5))]
    p_hex, amt, blind, _ = rct.scan_output(notes[0], w["a"], w["b"])
    for bad in ("x", 1.5, -3, "0x10", [1], True):
        spend = rct.make_spend(notes, 0, p_hex, amt, blind, [rct.output_for_spend(w["address"], "20")])
        spend["out"][0]["amt"] = bad
        with pytest.raises(sh.ShieldError, match="amount"):
            sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10)
    # a legit spend (no amt on outputs) still validates + applies cleanly
    spend = rct.make_spend(notes, 0, p_hex, amt, blind, [rct.output_for_spend(w["address"], "20")])
    assert all("amt" not in o for o in spend["out"])
    sh.apply_block(st, sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 11))


def test_v2_and_v3_coexist():
    """A transparent (v2) note and a confidential (v3) note live in the same sidecar without interfering."""
    st = _state()
    w = sh.new_keypair()
    v2note = sh.make_output(w["address"], "20")          # stage-2 transparent
    tx2 = _tx(sh.OP_MINT, json.dumps(v2note), recipient=sh.SHIELD_SINK, amount=str(v2note["amt"]))
    sh.apply_block(st, sh.validate_block(st, [tx2], 1))
    v3note = _mint3(st, w, 7, 2)                          # stage-3 confidential
    assert st.has_note(v2note["note_id"]) and st.has_note(v3note["note_id"])
    assert st.pool_units() == amounts.to_units("20") + amounts.to_units("7")
