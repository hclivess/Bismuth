"""Shielded value (doc/22) — live post-fork integration on regnet (shield=True, fork_signal=True).

Stage 1 (recipient privacy) + stage 2 (ring signatures for sender privacy). Proves the consensus path
the off-chain prototype could not:
  * mint moves transparent BIS into the pool and creates a stealth note backed 1:1 at SHIELD_SINK;
  * the recipient (and only the recipient) detects + opens a note by trial-decryption;
  * a RING spend consumes one note hidden among equal-amount decoys, with value conserved, recording a
    consensus key image — without revealing which note was spent;
  * a replayed spend of that note (same key image, even via a different ring) is REJECTED by consensus —
    the block does not advance the chain;
  * a reorg (rollback) clears the key image, making the note spendable again;
  * redeem moves value back to a transparent address; pool and sink move in lockstep throughout.

Run with: python3 -m pytest tests/test_shielded.py -v
"""
import json
import urllib.error
import urllib.request
from time import sleep

import amounts
import shieldedv1 as sh

API = "http://127.0.0.1:3031"


def _get(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def _note(nid):
    try:
        return _get("/api/shield/note/" + nid)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _pool(client):
    return _get("/api/shield/stats")["pool_units"]


def _sink(client):
    return amounts.to_units(client.balance(sh.SHIELD_SINK))


def _key_images():
    return _get("/api/shield/stats")["key_images"]


def _wait_fork_active(client):
    """Mine until hf2 has activated (shield: rules are inert before then), like the VM tests."""
    for _ in range(30):
        d = _get("/api/shield/stats")
        assert d["enabled"], "shield not enabled on this node"
        fh = d.get("fork_height")
        if fh is not None and client.block_height() > fh:
            return d
        client.mine(3)
    raise AssertionError("hf2 not active (tip %s, fork_height %s)"
                         % (client.block_height(), d.get("fork_height")))


def _ensure_balance(client, need_bis):
    for _ in range(50):
        if client.balance() >= need_bis + 5:
            return
        client.mine(3)
    raise AssertionError("could not fund wallet to %s BIS (have %s)" % (need_bis, client.balance()))


def _mine_until(client, predicate, rounds=12):
    """Mine in small bursts until `predicate()` holds — robust to mempool contention in the shared session
    (a fixed mine(N) can miss inclusion when other tests' txs compete for the 2-tx/block slots)."""
    for _ in range(rounds):
        if predicate():
            return
        client.mine(2)
        sleep(0.2)
    assert predicate(), "condition not reached after mining"


def _mint(client, address, amount):
    """Mint one note of `amount` BIS to `address`; wait until the sidecar indexes it; return the note."""
    note = sh.make_output(address, str(amount))
    pool0, sink0 = _pool(client), _sink(client)
    client.send(sh.SHIELD_SINK, amount, sh.OP_MINT, json.dumps(note))
    _mine_until(client, lambda: _note(note["note_id"]) is not None)
    # supply safety (delta form): a mint moves pool and sink by the SAME amount
    assert _pool(client) - pool0 == note["amt"], "pool not credited by mint"
    assert _sink(client) - sink0 == note["amt"], "sink not credited by mint (pool/sink desync)"
    return note


def _ring_of(notes):
    return [{"note_id": n["note_id"], "P": n["P"]} for n in notes]


def test_shield_enabled(client):
    d = _get("/api/shield/stats")
    assert d["enabled"] is True
    assert d["sink"] == sh.SHIELD_SINK


def test_shield_ring_spend_lifecycle(client):
    _wait_fork_active(client)
    _ensure_balance(client, 80)

    # --- MINT three equal-amount (20 BIS) notes to one wallet: the real note + two ring decoys --------
    wallet = sh.new_keypair()
    notes = [_mint(client, wallet["address"], 20) for _ in range(3)]
    keys = [sh.scan_output(n, wallet["a"], wallet["b"]) for n in notes]
    assert all(keys), "recipient failed to detect its own notes"
    stranger = sh.new_keypair()
    assert sh.scan_output(notes[0], stranger["a"], stranger["b"]) is None, "a stranger detected a note"

    # --- RING SPEND: spend notes[0] hidden in a ring over all three, split 20 -> 12 + 8 --------------
    r2, r3 = sh.new_keypair(), sh.new_keypair()
    o1 = sh.make_output(r2["address"], "12")
    o2 = sh.make_output(r3["address"], "8")
    spend = sh.make_spend(_ring_of(notes), real_index=0, p_hex=keys[0][0], outputs=[o1, o2])
    assert len(spend["ring"]) == 3 and spend["v"] == 2
    ki0, pool0, sink0 = _key_images(), _pool(client), _sink(client)
    client.send(client.address, 0, sh.OP_SPEND, json.dumps(spend))
    client.mine(2)
    sleep(0.4)

    assert _key_images() == ki0 + 1, "ring spend did not record a key image"
    assert _note(o1["note_id"])["amount"] == o1["amt"], "spend output not created"
    assert _note(o2["note_id"])["amount"] == o2["amt"]
    assert _pool(client) == pool0, "ring spend changed the pool value (must conserve)"
    assert _sink(client) == sink0, "ring spend touched the sink (must not)"

    # --- REDEEM notes[1] back out to a transparent address (ring over the same three) ----------------
    pool0, sink0 = _pool(client), _sink(client)
    redeem = sh.make_redeem(_ring_of(notes), real_index=1, p_hex=keys[1][0],
                            to_address=client.address, amount_units=notes[1]["amt"])
    client.send(client.address, 0, sh.OP_REDEEM, json.dumps(redeem))
    client.mine(2)
    sleep(0.4)

    assert _pool(client) - pool0 == -notes[1]["amt"], "pool not reduced by redeem"
    assert _sink(client) - sink0 == -notes[1]["amt"], "sink not debited by redeem (pool/sink desync)"


def test_shield_double_spend_rejected_and_reorg(client):
    _wait_fork_active(client)
    _ensure_balance(client, 40)

    # two equal-amount notes -> a ring of size 2; we own both (so we can spend either)
    wallet = sh.new_keypair()
    notes = [_mint(client, wallet["address"], 7) for _ in range(2)]
    keys = [sh.scan_output(n, wallet["a"], wallet["b"])[0] for n in notes]
    ring = _ring_of(notes)

    out = sh.make_output(sh.new_keypair()["address"], "7")
    spend = sh.make_spend(ring, real_index=0, p_hex=keys[0], outputs=[out])

    ki_before = _key_images()
    h_before_spend = client.block_height()
    client.send(client.address, 0, sh.OP_SPEND, json.dumps(spend))
    _mine_until(client, lambda: _key_images() > ki_before)      # the legit spend is on chain
    ki_after = _key_images()

    # --- DOUBLE SPEND: re-spend notes[0] via a DIFFERENT ring/outputs -> SAME key image -> rejected ---
    out2 = sh.make_output(sh.new_keypair()["address"], "7")
    spend_again = sh.make_spend(ring, real_index=0, p_hex=keys[0], outputs=[out2])  # same spent note
    assert spend_again["I"] == spend["I"], "key image should be identical for the same spent note"
    # clear the mempool FIRST so the next generated block contains ONLY the poison — otherwise, in the
    # shared session, an unrelated pending tx could fill the block (valid) and the height would advance.
    client.command("mpclear")
    sleep(0.2)
    height_before = client.block_height()
    client.send(client.address, 0, sh.OP_SPEND, json.dumps(spend_again))
    client.command("regtest_generate", [1])           # one attempt; the block must be rejected
    sleep(0.5)
    assert client.block_height() == height_before, "double-spend block was NOT rejected (chain advanced)"
    assert _key_images() == ki_after, "double-spend recorded a second key image"
    client.command("mpclear")                          # drop the poison tx so mining recovers
    sleep(0.2)

    # --- REORG: rolling back below the spend height clears the key image (note spendable again) -------
    client.rollback(h_before_spend + 1)
    sleep(0.3)
    assert _key_images() == ki_after - 1, "rollback did not drop the key image"
    # and the previously-spent note can now be spent again on the new branch
    out3 = sh.make_output(sh.new_keypair()["address"], "7")
    respend = sh.make_spend(ring, real_index=0, p_hex=keys[0], outputs=[out3])
    client.send(client.address, 0, sh.OP_SPEND, json.dumps(respend))
    _mine_until(client, lambda: _key_images() == ki_after)      # re-spend accepted on the new branch
