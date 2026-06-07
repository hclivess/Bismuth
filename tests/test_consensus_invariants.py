"""
Consensus-safety invariants on regnet — the non-negotiables, re-proven on EVERY test run.

You cannot: overspend, send a negative amount, spend without leaving room for the fee, spend from an
address whose key you don't hold (forged sender / tampered signature), or double-spend. Each check
crafts a malicious transaction, submits it to the live regnet node, and asserts the **end-to-end**
outcome: the recipient is never credited after mining (so it's caught by the mempool AND/OR the
digester — defense in depth — not just one layer). A positive control proves a legitimate tx *is*
accepted and credited, so the rejection tests can't pass vacuously.

These guard every future change (e.g. the LMDB storage rework): if any of them ever fails, the system
is not safe to ship. Run with: python3 -m pytest tests/test_consensus_invariants.py -v
"""
import time

import essentials

# valid 56-hex Bismuth addresses we do NOT control (a recipient and an unrelated third party)
RECIP = "deadbeef" * 7
OTHER = "cafebabe" * 7
TOL = 1e-6


def _sign(client, sender, recipient, amount, operation="", data=""):
    """Build + sign a tx (RSA, in-tree polysign) WITHOUT submitting. `sender` may be spoofed."""
    ts = "%.2f" % time.time()
    signed = essentials.sign_rsa(ts, sender, recipient, amount, operation, data,
                                 client.key, client.public_key_b64encoded)
    assert signed, "local signing failed"
    return list(signed)  # [ts, address, recipient, amount, signature, public_key, operation, openfield]


def _submit(client, tx):
    return client.command("mpinsert", [tx])


def _credited(client, addr, before):
    """Change in `addr`'s balance after submit+mine."""
    client.mine(2)
    time.sleep(0.3)
    return client.balance(addr) - before


# --- positive control: a legitimate tx MUST be accepted and credited ----------------------------

def test_valid_tx_is_accepted_and_credited(client):
    client.mine(2)
    before = client.balance(RECIP)
    _submit(client, _sign(client, client.address, RECIP, 2.5))
    assert abs(_credited(client, RECIP, before) - 2.5) < TOL, "a legitimate tx was not credited"


# --- the invariants: each violation must have ZERO effect ---------------------------------------

def test_no_overspend(client):
    client.mine(2)
    before = client.balance(RECIP)
    huge = client.balance() + 1_000_000.0          # far more than we own
    _submit(client, _sign(client, client.address, RECIP, huge))
    assert abs(_credited(client, RECIP, before)) < TOL, "recipient credited by an overspend"


def test_no_negative_amount(client):
    before = client.balance(RECIP)
    _submit(client, _sign(client, client.address, RECIP, -25.0))
    assert abs(_credited(client, RECIP, before)) < TOL, "recipient credited by a negative-amount tx"


def test_cannot_spend_without_room_for_fee(client):
    client.mine(2)
    before = client.balance(RECIP)
    _submit(client, _sign(client, client.address, RECIP, client.balance()))  # all of it, nothing for the fee
    assert abs(_credited(client, RECIP, before)) < TOL, "spend with no room for the fee was credited"


def test_signer_refuses_foreign_sender(client):
    # client-side guard: the in-tree signer will not even sign a tx whose sender we don't control.
    ts = "%.2f" % time.time()
    assert not essentials.sign_rsa(ts, OTHER, RECIP, 1.0, "", "",
                                   client.key, client.public_key_b64encoded), \
        "signer produced a signature for an address it does not own"


def test_no_spending_an_address_you_dont_control(client):
    # server-side guard: repoint a validly-signed tx's sender to an address we don't control. The
    # signature committed to OUR address, and our public key does not hash to OTHER -> the node rejects.
    before = client.balance(RECIP)
    tx = _sign(client, client.address, RECIP, 1.0)
    tx[1] = OTHER  # forge the sender on an otherwise-valid tx
    _submit(client, tx)
    assert abs(_credited(client, RECIP, before)) < TOL, "spent from an address we don't control"


def test_tampered_signature_rejected(client):
    tx = _sign(client, client.address, RECIP, 1.0)
    sig = tx[4]
    tx[4] = sig[:-4] + ("AAAA" if sig[-4:] != "AAAA" else "BBBB")  # corrupt the signature tail
    before = client.balance(RECIP)
    _submit(client, tx)
    assert abs(_credited(client, RECIP, before)) < TOL, "a tampered signature was accepted"


def test_no_double_spend_sequential(client):
    client.mine(2)
    before = client.balance(RECIP)
    tx = _sign(client, client.address, RECIP, 1.0)
    _submit(client, tx)
    assert abs(_credited(client, RECIP, before) - 1.0) < TOL, "valid tx not credited once"
    once = client.balance(RECIP)
    _submit(client, tx)                              # resubmit the EXACT same signed tx (already in ledger)
    assert abs(_credited(client, RECIP, once)) < TOL, "recipient credited twice (double-spend)"


def test_no_double_spend_same_block(client):
    client.mine(2)
    before = client.balance(RECIP)
    tx = _sign(client, client.address, RECIP, 1.0)
    _submit(client, tx)
    _submit(client, tx)                              # submit the identical tx twice before mining
    assert abs(_credited(client, RECIP, before) - 1.0) < TOL, "identical tx counted twice in one block"
