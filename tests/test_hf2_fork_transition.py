"""hf2 fork-transition end-to-end on regnet: the Ethereum-shape single-sig model activates at fork_height.

Proves the consensus gate across the boundary for an ordinary single-sig secp256k1 account:
  - PRE-fork: a recoverable/no-pubkey tx is REJECTED (the network still requires legacy buffer+pubkey).
  - POST-fork: a recoverable tx (signature signs the 32-byte content txid, public key dropped, ecrecover)
    is ACCEPTED and mined; and a LEGACY (buffer+pubkey) tx is now REJECTED.

Run with: python3 -m pytest tests/test_hf2_fork_transition.py -v
"""
import json
import time
import urllib.request

import bismuth_serialize
import hd_wallet
from polysign.signer import SignerType
from polysign.signerfactory import SignerFactory

API = "http://127.0.0.1:3031"


def _get(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def _fork_active():
    try:
        return bool(_get("/api/fork").get("active"))
    except Exception:
        return False


def _wait_fork_active(client, tries=30):
    for _ in range(tries):
        if _fork_active():
            return
        client.mine(3)
    raise AssertionError("hf2 fork did not activate on regnet")


def _ecdsa_balance(client, addr):
    return float(client.command("balanceget", [addr])[0])


def _send_and_mine(client, tx, blocks=3):
    client.command("mpinsert", [list(tx)])
    client.mine(blocks)
    time.sleep(0.3)


def test_hf2_single_sig_fork_transition(client):
    signer = SignerFactory.from_seed("c" * 64, signer_type=SignerType.ECDSA)
    addr = signer.address()
    assert SignerFactory.is_single_sig_ecdsa(addr)

    # Fund the ECDSA account from the RSA wallet's existing coinbase (the fixture already mined 2 blocks
    # ~= 25 BIS). No extra mining, so we stay well below the regnet fork activation height (window=5,
    # activates ~height 10) and the PRE-fork case is exercised deterministically.
    client.send(addr, 15)
    client.mine(2)
    deadline = time.time() + 15
    while _ecdsa_balance(client, addr) < 15 and time.time() < deadline:
        client.mine(1)
    assert _ecdsa_balance(client, addr) >= 15, "ECDSA account was not funded"

    # --- PRE-FORK: a recoverable / dropped-pubkey tx must be REJECTED -----------------------------
    # Only exercisable while the chain is still pre-fork (this file run standalone, or first among the
    # fork tests). In a FULL-SUITE run an earlier test may have already activated hf2 on the shared regnet
    # node; then we skip straight to the post-fork cases below (which are the load-bearing ones).
    if not _fork_active():
        bal = _ecdsa_balance(client, addr)
        pre = hd_wallet.sign_transaction(signer, "%.2f" % time.time(), addr, client.address,
                                         "3.0", post_fork=True)   # new-scheme tx, but we're pre-fork
        _send_and_mine(client, pre)
        assert abs(_ecdsa_balance(client, addr) - bal) < 0.5, \
            "pre-fork node wrongly accepted a recoverable (post-fork-scheme) tx"

    # --- activate hf2 ----------------------------------------------------------------------------
    _wait_fork_active(client)

    # --- POST-FORK: a recoverable tx (signs the txid, no pubkey) is ACCEPTED and mined -----------
    bal_before = _ecdsa_balance(client, addr)
    tx = hd_wallet.sign_transaction(signer, "%.2f" % time.time(), addr, client.address,
                                    "5.0", post_fork=True)
    assert tx[5] == "" and len(tx[4]) == 130, "tx is not in recoverable/dropped-pubkey wire form"
    expected_txid = bismuth_serialize.tx_id(tx[0], tx[1], tx[2], tx[3], tx[6], tx[7])
    _send_and_mine(client, tx)
    deadline = time.time() + 30                       # generous: the tx must be mined AND digested
    while _ecdsa_balance(client, addr) > bal_before - 4 and time.time() < deadline:
        client.mine(1); time.sleep(0.4)              # let the async digest catch up before re-reading
    bal_after = _ecdsa_balance(client, addr)
    assert bal_after <= bal_before - 5, \
        "post-fork recoverable tx was not accepted/mined (balance %s -> %s)" % (bal_before, bal_after)

    assert len(expected_txid) == 64
    # It is on chain with the recoverable signature stored (dropped-pubkey wire form persisted), and the
    # post-fork tx surfaces its content-hash txid (not signature[:56]) in the address listing. POLL: under
    # full-suite load the address index lags the mined block and a transient re-org can briefly surface a
    # stale row, so re-fetch until the tx (matched by its unique signature) shows the canonical txid.
    onchain = []
    deadline = time.time() + 20
    while time.time() < deadline:
        recip_txs = _get("/api/address/%s/transactions?limit=50" % client.address).get("transactions", [])
        onchain = [t for t in recip_txs if t.get("signature") == tx[4]]
        if onchain and onchain[0].get("txid") == expected_txid:
            break
        time.sleep(0.5)
    assert onchain, "the recoverable tx is not retrievable on chain"
    assert onchain[0].get("txid") == expected_txid, ("address listing txid", onchain[0].get("txid"), expected_txid)
    # and it is retrievable BY that 64-hex content txid via the shape-dispatched /api/transaction lookup
    looked = _get("/api/transaction/%s" % expected_txid)
    assert looked.get("txid") == expected_txid, ("content-txid lookup mismatch", looked)
    assert looked.get("address") == addr and looked.get("recipient") == client.address, looked
    assert looked.get("signature") == tx[4], "looked-up tx is not the recoverable one"

    # --- POST-FORK: a LEGACY (buffer+pubkey) single-sig tx is now REJECTED ------------------------
    bal_before2 = _ecdsa_balance(client, addr)
    legacy = hd_wallet.sign_transaction(signer, "%.2f" % time.time(), addr, client.address,
                                        "7.0", post_fork=False)   # old-scheme tx, but we're post-fork
    assert legacy[5] != "", "legacy tx should still carry the public key"
    _send_and_mine(client, legacy)
    assert abs(_ecdsa_balance(client, addr) - bal_before2) < 0.5, \
        "post-fork node wrongly accepted a legacy (pre-fork-scheme) single-sig tx"
