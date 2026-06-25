"""KV-seam tests for the shielded sidecar store (``shieldedv1.ShieldedState``, doc/26 storage stage 1).

After migrating ShieldedState off a direct ``lmdb.open()`` onto ``kvstore.open_store`` these pin the two
properties the migration must preserve:

  (a) BYTE-IDENTITY — the LMDB env the migrated store writes is byte-for-byte what the pre-migration
      direct-lmdb store wrote: the same 6 named sub-dbs and the same raw key/value layout
      (note_id->json, height||note_id->b"", image->height, height||image->b"", height||seq->delta,
      meta counters as decimal strings). A running node reads its existing files unchanged.
  (b) SWAPPABILITY — the SAME ShieldedState run through open_store('sqlite-kv', ...) yields IDENTICAL
      observable results (notes, key images, pool, stats, rollback) to the lmdb backend.

Node-free: drives the real mint/spend/redeem/rollback path via validate_block/apply_block on a fresh
tmp_path store, so no running node and no prod ledger is touched. Needs only the ``lmdb`` dependency.

Run with: python3 -m pytest tests/test_shielded_kvstore.py -v
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

pytest.importorskip("lmdb")

import amounts        # noqa: E402
import shieldedv1 as sh  # noqa: E402


def _tx(op, openfield, recipient="x" * 56, amount="0"):
    # the digester's 12-field tuple: [3]=recipient [4]=amount [10]=operation [11]=openfield
    return ("1", "ts", "s" * 56, recipient, amount, "sig", "pub", "bh", "0", "0", op, openfield)


def _mint(st, wallet, amount_bis, height):
    note = sh.make_output(wallet["address"], str(amount_bis))
    tx = _tx(sh.OP_MINT, json.dumps(note), recipient=sh.SHIELD_SINK, amount=str(note["amt"]))
    sh.apply_block(st, sh.validate_block(st, [tx], height))
    return note


def _drive(backend, path):
    """Identical shielded workload against a ShieldedState on the given backend; return observable results
    over the full read surface (note, has_note, has_key_image, pool_units, stats) across mint, ring spend,
    redeem, and a reorg rollback."""
    amounts.LEDGER_INTEGER = True
    st = sh.ShieldedState(path, map_size=64 * 1024 * 1024, backend=backend)
    try:
        w = sh.new_keypair()
        # mint two equal-amount notes (a ring of 2) at heights 1,2
        notes = [_mint(st, w, 20, 1 + i) for i in range(2)]
        keys = [sh.scan_output(n, w["a"], w["b"])[0] for n in notes]
        ring = [{"note_id": n["note_id"], "P": n["P"]} for n in notes]

        # ring spend notes[0] -> a fresh output, at height 5
        out = sh.make_output(sh.new_keypair()["address"], "20")
        spend = sh.make_spend(ring, 0, keys[0], [out])
        sh.apply_block(st, sh.validate_block(st, [_tx(sh.OP_SPEND, json.dumps(spend))], 5))
        spend_image = spend["I"]

        # redeem notes[1] back to a transparent address, at height 7
        redeem = sh.make_redeem(ring, 1, keys[1], "y" * 56, notes[1]["amt"])
        payouts = sh.apply_block(st, sh.validate_block(st, [_tx(sh.OP_REDEEM, json.dumps(redeem))], 7))
        redeem_image = redeem["I"]

        # The crypto keys are random per run (os.urandom), so compare DETERMINISTIC observables only: the
        # stored note's reorg-safe shape (create_height/token/amount, minus the random P/R/memo/note_id), the
        # spent-set membership, pool/stats/payout amounts, and the rollback effects. These are the values the
        # seam must reproduce identically on any backend.
        def _note_shape(nd):
            if nd is None:
                return None
            return {"create_height": nd["create_height"], "token": nd["token"], "amount": nd["amount"]}

        units20 = amounts.to_units("20")
        snapshot = {
            "note0_shape": _note_shape(st.note(notes[0]["note_id"])),
            "note1_shape": _note_shape(st.note(notes[1]["note_id"])),
            "out_note_shape": _note_shape(st.note(out["note_id"])),
            "has_note0": st.has_note(notes[0]["note_id"]),
            "has_spend_image": st.has_key_image(spend_image),
            "has_redeem_image": st.has_key_image(redeem_image),
            "pool": st.pool_units(),
            "stats": st.stats(),
            "payout_amount": payouts[0][1] if payouts else None,
            "payout_count": len(payouts),
            "units20": units20,
        }

        # reorg: roll back below height 5 -> spend + redeem key images cleared, their flow/notes dropped
        st.rollback_under(5)
        snapshot["after_has_spend_image"] = st.has_key_image(spend_image)
        snapshot["after_has_redeem_image"] = st.has_key_image(redeem_image)
        snapshot["after_has_out_note"] = st.has_note(out["note_id"])
        snapshot["after_pool"] = st.pool_units()
        snapshot["after_stats"] = st.stats()
        # the two original mint notes (heights 1,2) survive the rollback to 5
        snapshot["after_has_note0"] = st.has_note(notes[0]["note_id"])
        return snapshot
    finally:
        st.close()


# --------------------------------------------------------------------------- (a) PARITY: byte-identical
def test_on_disk_bytes_identical_to_direct_lmdb(tmp_path):
    """After migrating onto kvstore.open_store, the LMDB env ShieldedState writes must be BYTE-IDENTICAL to
    what the pre-migration direct-lmdb store wrote: the same 6 named sub-dbs and the same raw key/value
    layout. Reconstructs the expected bytes from the store's own encoding helpers and compares the raw env."""
    import lmdb

    amounts.LEDGER_INTEGER = True
    path = str(tmp_path / "shielded")
    st = sh.ShieldedState(path, map_size=64 * 1024 * 1024)
    w = sh.new_keypair()
    try:
        note = sh.make_output(w["address"], "20")
        tx = _tx(sh.OP_MINT, json.dumps(note), recipient=sh.SHIELD_SINK, amount=str(note["amt"]))
        sh.apply_block(st, sh.validate_block(st, [tx], 3))
        nid = note["note_id"]
        height = 3
    finally:
        st.close()

    # read the raw on-disk bytes back out of the env the store wrote, per named sub-db
    env = lmdb.open(path, subdir=True, max_dbs=6, readonly=True, lock=False)
    on_disk = {}
    for name in (b"notes", b"notes_h", b"kimg", b"kimg_h", b"flows", b"meta"):
        db = env.open_db(name, create=False)
        with env.begin() as t:
            on_disk[name.decode()] = {bytes(k): bytes(v) for k, v in t.cursor(db=db)}
    env.close()

    hbe = sh._hbe
    kb = nid.encode()
    payload = {"h": height, "tok": note["tok"], "amt": int(note["amt"]),
               "R": note["R"], "P": note["P"], "memo": note.get("memo", ""), "C": note["c"]}

    # mint: one note, no key images, one +amt flow (seq 1), meta counters as decimal strings
    assert on_disk["notes"] == {kb: json.dumps(payload).encode()}
    assert on_disk["notes_h"] == {hbe(height) + kb: b""}
    assert on_disk["kimg"] == {}
    assert on_disk["kimg_h"] == {}
    assert on_disk["flows"] == {hbe(height) + hbe(1): str(int(note["amt"])).encode()}
    assert on_disk["meta"] == {
        b"pool": str(int(note["amt"])).encode(),
        b"nnotes": b"1",
        b"nki": b"0",
        b"flowseq": b"1",
    }


# --------------------------------------------------------------------------- (b) SWAPPABILITY
@pytest.mark.parametrize("backend", ["lmdb", "sqlite-kv"])
def test_backend_swappable_smoke(backend, tmp_path):
    """Each backend independently produces the expected shielded results through the open_store(backend=...)
    seam (mint/spend/redeem + reorg rollback)."""
    res = _drive(backend, str(tmp_path / ("shielded_" + backend)))
    u20 = res["units20"]
    assert res["has_spend_image"] is True and res["has_redeem_image"] is True
    assert res["out_note_shape"] is not None and res["out_note_shape"]["amount"] == res["note0_shape"]["amount"]
    # mint two 20s (pool +2*u20), spend is value-neutral, redeem -u20 -> pool == u20
    assert res["pool"] == u20
    assert res["stats"] == {"notes": 3, "key_images": 2, "pool_units": u20}
    assert res["payout_count"] == 1 and res["payout_amount"] == res["note1_shape"]["amount"]
    # after rollback below 5: both spend+redeem images gone, the spend output note gone, pool back to +2*u20
    assert res["after_has_spend_image"] is False and res["after_has_redeem_image"] is False
    assert res["after_has_out_note"] is False
    assert res["after_pool"] == 2 * u20
    assert res["after_stats"] == {"notes": 2, "key_images": 0, "pool_units": 2 * u20}
    assert res["after_has_note0"] is True


def test_swappability_lmdb_equals_sqlite(tmp_path):
    """The SAME ShieldedState run on a 2nd engine (sqlite-kv) yields IDENTICAL observable results to lmdb —
    proving the KV seam is engine-independent for this store (incl. the height range-scan rollback and the
    meta counters / pool flows). [doc/26 storage stage 1]"""
    res_lmdb = _drive("lmdb", str(tmp_path / "shielded_lmdb"))
    res_sqlite = _drive("sqlite-kv", str(tmp_path / "shielded_sqlite"))
    assert res_lmdb == res_sqlite, "ShieldedState results differ across backends -> seam not engine-independent"
