"""Consensus-rule unit tests for shielded validation (doc/22) — drives validate_block directly with a
fabricated block, no node needed. Fast coverage of the rejection paths, including the inflation guard.

Run with: python3 -m pytest tests/test_shielded_validation.py -v
"""
import json
import os
import tempfile

import pytest

import amounts
import shieldedv1 as sh


def _state():
    amounts.LEDGER_INTEGER = True
    return sh.ShieldedState(os.path.join(tempfile.mkdtemp(), "shielded-test.db"))


def _tx(op, openfield, recipient="x" * 56, amount="0"):
    # the digester's 12-field tuple: [3]=recipient [4]=amount [10]=operation [11]=openfield
    return ("1", "ts", "s" * 56, recipient, amount, "sig", "pub", "bh", "0", "0", op, openfield)


def _mint(st, wallet, amount_bis, height):
    note = sh.make_output(wallet["address"], str(amount_bis))
    tx = _tx(sh.OP_MINT, json.dumps(note), recipient=sh.SHIELD_SINK, amount=str(note["amt"]))
    sh.apply_block(st, sh.validate_block(st, [tx], height))
    return note


def test_legit_ring_spend_passes():
    st = _state()
    w = sh.new_keypair()
    notes = [_mint(st, w, 20, 1 + i) for i in range(2)]
    keys = [sh.scan_output(n, w["a"], w["b"])[0] for n in notes]
    ring = [{"note_id": n["note_id"], "P": n["P"]} for n in notes]
    out = sh.make_output(sh.new_keypair()["address"], "20")
    spend = sh.make_spend(ring, 0, keys[0], [out])
    parsed = sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10)
    assert parsed and parsed[0][0] == "spend"


def test_negative_output_is_rejected_inflation_guard():
    """[{amt:A+k},{amt:-k}] conserves the total but would mint an over-valued note -> inflation."""
    st = _state()
    w = sh.new_keypair()
    note = _mint(st, w, 20, 1)
    p = sh.scan_output(note, w["a"], w["b"])[0]
    ring = [{"note_id": note["note_id"], "P": note["P"]}]
    big = sh.make_output(sh.new_keypair()["address"], "25")    # +25
    neg = sh.make_output(sh.new_keypair()["address"], "-5")    # -5  -> sum == 20, but negative output
    spend = sh.make_spend(ring, 0, p, [big, neg])
    with pytest.raises(sh.ShieldError):
        sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10)


def test_zero_output_is_rejected():
    st = _state()
    w = sh.new_keypair()
    note = _mint(st, w, 20, 1)
    p = sh.scan_output(note, w["a"], w["b"])[0]
    ring = [{"note_id": note["note_id"], "P": note["P"]}]
    full = sh.make_output(sh.new_keypair()["address"], "20")
    zero = sh.make_output(sh.new_keypair()["address"], "0")
    spend = sh.make_spend(ring, 0, p, [full, zero])
    with pytest.raises(sh.ShieldError):
        sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10)


def test_non_conserving_spend_is_rejected():
    st = _state()
    w = sh.new_keypair()
    note = _mint(st, w, 20, 1)
    p = sh.scan_output(note, w["a"], w["b"])[0]
    ring = [{"note_id": note["note_id"], "P": note["P"]}]
    spend = sh.make_spend(ring, 0, p, [sh.make_output(sh.new_keypair()["address"], "19")])  # 19 != 20
    with pytest.raises(sh.ShieldError):
        sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10)


def test_non_uniform_ring_is_rejected():
    st = _state()
    w = sh.new_keypair()
    n20 = _mint(st, w, 20, 1)
    n10 = _mint(st, w, 10, 2)
    p = sh.scan_output(n20, w["a"], w["b"])[0]
    ring = [{"note_id": n20["note_id"], "P": n20["P"]}, {"note_id": n10["note_id"], "P": n10["P"]}]
    spend = sh.make_spend(ring, 0, p, [sh.make_output(sh.new_keypair()["address"], "20")])
    with pytest.raises(sh.ShieldError):
        sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10)


def test_double_spend_same_key_image_rejected():
    st = _state()
    w = sh.new_keypair()
    notes = [_mint(st, w, 20, 1 + i) for i in range(2)]
    keys = [sh.scan_output(n, w["a"], w["b"])[0] for n in notes]
    ring = [{"note_id": n["note_id"], "P": n["P"]} for n in notes]
    spend = sh.make_spend(ring, 0, keys[0], [sh.make_output(sh.new_keypair()["address"], "20")])
    sh.apply_block(st, sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10))
    again = sh.make_spend(ring, 0, keys[0], [sh.make_output(sh.new_keypair()["address"], "20")])
    with pytest.raises(sh.ShieldError):
        sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(again))], 11)


def test_mint_non_positive_rejected():
    st = _state()
    w = sh.new_keypair()
    note = sh.make_output(w["address"], "0")
    tx = _tx(sh.OP_MINT, json.dumps(note), recipient=sh.SHIELD_SINK, amount="0")
    with pytest.raises(sh.ShieldError):
        sh.validate_block(st, [tx], 1)


# --- adversarial-pass regression tests (each guards a hole found auditing validate_block) ----------
import coincurve as cc


def _ring2(st, w, amount):
    notes = [_mint(st, w, amount, 1 + i) for i in range(2)]
    keys = [sh.scan_output(n, w["a"], w["b"])[0] for n in notes]
    return notes, keys, [{"note_id": n["note_id"], "P": n["P"]} for n in notes]


def test_key_image_canonicalization_double_spend_rejected():
    """The same key image submitted UNCOMPRESSED must not slip past the (compressed) spent-set."""
    st = _state()
    w = sh.new_keypair()
    notes, keys, ring = _ring2(st, w, 9)
    spend = sh.make_spend(ring, 0, keys[0], [sh.make_output(sh.new_keypair()["address"], "9")])
    sh.apply_block(st, sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10))
    # craft the SAME spent note's image in uncompressed form, re-signed so the bad encoding "verifies"
    ring_pubs = [cc.PublicKey(bytes.fromhex(n["P"])) for n in ring]
    x = int(keys[0], 16)
    uncompressed = sh.key_image(x, ring_pubs[0]).format(compressed=False).hex()
    out2 = sh.make_output(sh.new_keypair()["address"], "9")
    payload = sh._spend_payload([out2])
    msg = sh._ring_message([n["note_id"] for n in ring], uncompressed, payload)
    _, c, r = sh.ring_sign(msg, ring_pubs, x, 0)
    spend2 = {"v": 2, "ring": [n["note_id"] for n in ring], "I": uncompressed,
              "c": [sh._sb(ci).hex() for ci in c], "r": [sh._sb(ri).hex() for ri in r], "out": [out2]}
    with pytest.raises(sh.ShieldError):
        sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend2))], 11)


def test_mint_missing_field_rejected():
    """A note missing a field apply_block reads must fail validation (else apply desyncs the sidecar)."""
    st = _state()
    note = sh.make_output(sh.new_keypair()["address"], "5")
    del note["R"]
    tx = _tx(sh.OP_MINT, json.dumps(note), recipient=sh.SHIELD_SINK, amount=str(note["amt"]))
    with pytest.raises(sh.ShieldError):
        sh.validate_block(st, [tx], 1)


def test_spend_output_missing_field_rejected():
    st = _state()
    w = sh.new_keypair()
    notes, keys, ring = _ring2(st, w, 20)
    out = sh.make_output(sh.new_keypair()["address"], "20")
    spend = sh.make_spend(ring, 0, keys[0], [out])
    del spend["out"][0]["R"]
    with pytest.raises(sh.ShieldError):
        sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10)


def test_duplicate_output_note_rejected():
    """Two outputs with the same P (note_id) — INSERT OR IGNORE would silently drop one, losing value."""
    st = _state()
    w = sh.new_keypair()
    notes, keys, ring = _ring2(st, w, 20)
    o = sh.make_output(sh.new_keypair()["address"], "10")
    spend = sh.make_spend(ring, 0, keys[0], [o, o])     # same output dict twice -> same note_id
    with pytest.raises(sh.ShieldError):
        sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10)


def test_redeem_to_sink_rejected():
    st = _state()
    w = sh.new_keypair()
    notes, keys, ring = _ring2(st, w, 8)
    redeem = sh.make_redeem(ring, 0, keys[0], sh.SHIELD_SINK, notes[0]["amt"])
    with pytest.raises(sh.ShieldError):
        sh.validate_block(st, [_tx(sh.OP_REDEEM, json.dumps(redeem))], 10)


def test_float_output_amount_rejected():
    st = _state()
    w = sh.new_keypair()
    note = _mint(st, w, 20, 1)
    p = sh.scan_output(note, w["a"], w["b"])[0]
    ring = [{"note_id": note["note_id"], "P": note["P"]}]
    spend = sh.make_spend(ring, 0, p, [sh.make_output(sh.new_keypair()["address"], "20")])
    spend["out"][0]["amt"] = 20.0       # float, not int
    with pytest.raises(sh.ShieldError):
        sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 10)
