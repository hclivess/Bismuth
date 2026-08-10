"""Transaction behaviour tests against a regnet node."""
# Transaction behavior on regnet. The regnet node + client come from conftest.py fixtures.
# Run with: python3 -m pytest -v

import json
import os
import urllib.request
from time import sleep

EXTERNAL = "8342c1610de5d7aa026ca7ae6d21bd99b1b3a4654701751891f08742"

# The node's REST port (conftest starts it on 3031 unless the harness re-ports it).
REST_PORT = int(os.environ.get("BISMUTH_REST_API_PORT", "3031"))


def _rest(path):
    with urllib.request.urlopen("http://127.0.0.1:%d/api/%s" % (REST_PORT, path), timeout=8) as r:
        return json.load(r)


def test_amount_and_recipient(client):
    client.mine(1)
    client.send(client.address, 1.0)
    client.mine(1)
    sleep(0.5)
    tx = client.latest_transactions(num=1)
    assert float(tx[0]["amount"]) == 1.0
    assert tx[0]["recipient"] == client.address


def test_tx_id(client):
    client.mine(1)
    txid = client.send(client.address, 1.0)
    client.mine(1)
    sleep(0.5)
    tx = client.latest_transactions(num=1)
    assert tx[0]["signature"][:56] == txid


def test_operation_and_openfield(client):
    operation, data = "test:1", "Bismuth"
    client.mine(1)
    client.send(client.address, 0.0, operation=operation, data=data)
    client.mine(1)
    sleep(0.5)
    tx = client.latest_transactions(num=1)
    assert tx[0]["operation"] == operation
    assert tx[0]["openfield"] == data


def test_fee(client):
    import json
    import urllib.request
    data = "1234567890" * 5  # 50 chars
    client.mine(1)
    client.send(client.address, 0.0, data=data)
    client.mine(1)
    sleep(0.5)
    tx = client.latest_transactions(num=1)
    # post-fork the base fee is demand-responsive (fee_dynamics) — read it rather than assume the constant
    base = float(_rest("fee")["base_fee"])
    assert abs(float(tx[0]["fee"]) - (base + 1e-5 * len(data))) < 1e-9


def test_operation_length_capped_per_era(client):
    """PRE-fork `operation` is truncated to 30 chars; POST-fork it is free-form and uncapped (hf2,
    doc/41 — see the `_post_fork` gate in digest_tx.py / digest.py). Assert the rule for the era the
    chain is actually in, so this stays honest on both sides of the fork."""
    client.mine(1)
    op = "1" * 31                                        # one over the legacy 30-char limit
    client.send(client.address, 0.0, operation=op)
    client.mine(1)
    sleep(0.5)
    tx = client.latest_transactions(num=1)
    fh = _rest("fork").get("fork_height")
    post_fork = fh is not None and client.block_height() >= int(fh)
    if post_fork:
        assert tx[0]["operation"] == op, "post-fork operation must be stored uncapped"
    else:
        assert len(tx[0]["operation"]) <= 30


def test_sender_and_recipient_balances(client):
    client.mine(1)
    sender_before = client.balance()
    recipient_before = client.balance(EXTERNAL)
    client.send(EXTERNAL, 1.0)
    client.mine(1)
    sleep(0.5)
    txs = client.latest_transactions(num=2)  # this block's coinbase + the send tx
    reward = sum(float(t["reward"]) for t in txs)  # only the coinbase has reward
    fee = sum(float(t["fee"]) for t in txs)        # only the send has fee
    sender_after = client.balance()
    recipient_after = client.balance(EXTERNAL)
    # miner == sender in regnet, so fees come back in the reward; net effect is -1.0
    assert abs((sender_after - sender_before - reward + fee) + 1.0) < 1e-6
    assert abs((recipient_after - recipient_before) - 1.0) < 1e-9


def test_send_more_than_owned_rejected(client):
    client.mine(1)
    balance = client.balance()
    client.send(EXTERNAL, balance)  # no room for the fee -> mempool rejects
    client.mine(1)
    sleep(0.5)
    assert client.balance() > 1.0


def test_two_txs_exceeding_balance_rejected(client):
    client.mine(1)
    balance = client.balance()
    client.send(EXTERNAL, 1.0)
    client.send(EXTERNAL, balance - 1.0)  # together exceed balance (after fees)
    client.mine(1)
    sleep(0.5)
    assert client.balance() > 1.0
