"""Regression tests for the 2026-06 mint-from-thin-air audit (doc/47).

Each test reproduces the ACTUAL exploit the audit confirmed and asserts the fix rejects it:

  * CRITICAL — ringct.verify_redeem field-wraparound: the commitment opening + MLSAG bind the redeemed
    amount only MOD N, so an attacker could redeem amt = a_real + k·N (congruent mod N) and mint ~2^256
    transparent units at the shielded->transparent boundary. Fixed by bounding amt to [1, 2^RANGE_BITS).
  * CRITICAL — v3<->v2 cross-version redemption: a confidential (v3) note's stored integer amount is
    meaningless (value is in its Pedersen commitment), but the v2 transparent path pays out that stored
    amount. An attacker could mint an arbitrary stored amount on a v3 spend output and redeem it via v2.
    Fixed by tagging notes with their version and barring v3 notes from v2 rings (and vice versa), plus
    rejecting any public amount on a confidential spend output.

Run with: python3 -m pytest tests/test_inflation_audit_fixes.py -v
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
    return sh.ShieldedState(os.path.join(tempfile.mkdtemp(), "shielded-audit-test.db"))


def _tx(op, openfield, recipient="x" * 56, amount="0"):
    return ("1", "ts", "s" * 56, recipient, amount, "sig", "pub", "bh", "0", "0", op, openfield)


def _mint3(st, wallet, amount_bis, height):
    note = rct.make_output(wallet["address"], str(amount_bis))
    tx = _tx(sh.OP_MINT, json.dumps(note), recipient=sh.SHIELD_SINK, amount=str(note["amt"]))
    sh.apply_block(st, sh.validate_block(st, [tx], height))
    return note


# --- CRITICAL 1: RingCT redeem field-wraparound (mod-N) inflation ----------------------------------
def test_v3_redeem_field_wraparound_rejected():
    st = _state()
    w = sh.new_keypair()
    notes = [_mint3(st, w, 20, 1 + i) for i in range(2)]
    p_hex, amt, blind, _ = rct.scan_output(notes[1], w["a"], w["b"])

    # control: the honest redeem of the real amount validates (validate_block does not apply, so no key
    # image is burned and we can reuse the note for the malicious case below)
    good = rct.make_redeem(notes, 1, p_hex, amt, blind, "d" * 56)
    assert sh.validate_block(st, [_tx(sh.OP_REDEEM, json.dumps(good))], 10)

    # EXPLOIT: redeem amt + N. commit(amt+N, b) == commit(amt, b) (commit reduces mod N) and the MLSAG is
    # signed over the inflated amount, so the opening and the signature BOTH still verify — only the new
    # explicit range bound stops it. Without the fix this returns ~2^256 spendable units.
    evil = rct.make_redeem(notes, 1, p_hex, amt + rct._N, blind, "e" * 56)
    assert evil["amt"] >= (1 << rct.RANGE_BITS), "exploit amount must exceed the range bound"
    # ringct.verify_redeem itself must reject it
    with pytest.raises(ValueError, match="out of range"):
        rct.verify_redeem([{"note_id": n["note_id"], "p_pub": n["P"], "commitment": n["C"]} for n in notes], evil)
    # and so must the consensus validator
    with pytest.raises(sh.ShieldError, match="range"):
        sh.validate_block(st, [_tx(sh.OP_REDEEM, json.dumps(evil))], 11)


# --- CRITICAL 2: v3 <-> v2 cross-version redemption -------------------------------------------------
def test_v3_note_cannot_enter_v2_transparent_ring():
    """The core of the cross-version exploit: a confidential note resolved by the v2 path (which pays its
    stored integer amount). Must be refused before any signature check."""
    st = _state()
    w = sh.new_keypair()
    v3 = _mint3(st, w, 7, 1)
    with pytest.raises(sh.ShieldError, match="not a transparent note"):
        sh._resolve_ring(st, {"ring": [v3["note_id"]]}, 5)


def test_v2_note_cannot_enter_v3_confidential_ring():
    """Symmetric segregation: a transparent note must not be usable as a RingCT ring member."""
    st = _state()
    w = sh.new_keypair()
    v2 = sh.make_output(w["address"], "20")
    sh.apply_block(st, sh.validate_block(
        st, [_tx(sh.OP_MINT, json.dumps(v2), recipient=sh.SHIELD_SINK, amount=str(v2["amt"]))], 1))
    with pytest.raises(sh.ShieldError, match="not a confidential note"):
        sh._resolve_ring_v3(st, [v2["note_id"]], 5)


def test_v3_spend_output_rejects_injected_public_amount():
    """The attacker's first step was attaching a (valid) public amount to a confidential spend output so it
    would be stored and later redeemed transparently. A confidential output must carry no public amount."""
    st = _state()
    w = sh.new_keypair()
    notes = [_mint3(st, w, v, 1 + i) for i, v in enumerate((20, 5))]
    p_hex, amt, blind, _ = rct.scan_output(notes[0], w["a"], w["b"])
    spend = rct.make_spend(notes, 0, p_hex, amt, blind, [rct.output_for_spend(w["address"], "20")])
    spend["out"][0]["amt"] = 10 ** 18                 # a VALID non-negative int (slips past the junk-amt check)
    with pytest.raises(sh.ShieldError, match="must not carry a public amount"):
        sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10)


def test_notes_carry_version_tag():
    """The fix's foundation: stored notes are tagged v2 (transparent) / v3 (confidential)."""
    st = _state()
    w = sh.new_keypair()
    v2 = sh.make_output(w["address"], "20")
    sh.apply_block(st, sh.validate_block(
        st, [_tx(sh.OP_MINT, json.dumps(v2), recipient=sh.SHIELD_SINK, amount=str(v2["amt"]))], 1))
    v3 = _mint3(st, w, 7, 2)
    assert st.note(v2["note_id"])["version"] == 2
    assert st.note(v3["note_id"])["version"] == 3
