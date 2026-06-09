"""
Demo "professional" contracts — live post-fork integration on regnet (vm=True, fork_signal=True).

Drives three hand-authored RV32I contracts end-to-end through real vm:deploy / vm:call transactions
mined past the hf2 fork, proving the VM's range:

  * ESCROW  (contracts/escrow.py)  — access control + custody: buyer funds, arbiter releases to seller.
  * VESTING (contracts/vesting.py) — height gating via SYS_NUMBER: release reverts before the unlock
                                     height, succeeds at/after it, paying the beneficiary from custody.
  * TOKEN   (contracts/token_contract.py) — pure on-chain accounting: mint a fixed supply, move balances.

Exhaustive edge cases are in tests/test_contracts_offchain.py; this file proves the chain plumbing.

Run with: python3 -m pytest tests/test_vm_demo_contracts.py -v
"""
import json
import urllib.request
from time import sleep

import escrow
import token_contract as tok
import vesting

API = "http://127.0.0.1:3031"
UNIT = 100_000_000


def _get(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def _wait_fork_active(client):
    info = _get("/api/vm/contracts")
    assert info["enabled"], "vm not enabled on this node"
    for _ in range(25):
        info = _get("/api/vm/contracts")
        if info.get("fork_height") is not None and client.block_height() > info["fork_height"]:
            return info
        client.mine(3)
    raise AssertionError("hf2 fork not active")


def _deploy(client, code_hex):
    before = set(_get("/api/vm/contracts")["contracts"])
    client.send(client.address, 1, "vm:deploy", code_hex)
    client.mine(2)
    sleep(0.3)
    new = set(_get("/api/vm/contracts")["contracts"]) - before
    assert new, "deploy mined but the VM did not register the contract"
    return new.pop()


def _call(client, addr, calldata_hex, amount=0):
    import vm_engine
    recipient = vm_engine.VM_SINK if amount else client.address
    client.send(recipient, amount, "vm:call", "%s:%s" % (addr, calldata_hex))
    client.mine(1)
    sleep(0.2)


def _slots(addr):
    d = _get("/api/vm/contract/" + addr)
    return {int(s["key"]): int(s["value"]) for s in d["storage"]}, d


def _bal(addr):
    return int(_get("/api/vm/contract/" + addr)["balance"])


def _sel(n):
    return n.to_bytes(4, "big").hex()


def test_escrow_release(client):
    """Buyer (the wallet) funds; the wallet is also the arbiter here, so it can RELEASE to the seller."""
    _wait_fork_active(client)
    wallet = client.address
    me = escrow.party_id_of(wallet)
    seller_id = (me ^ 0x0F0F0F0F) & 0xFFFFFFFF                # a distinct seller id (never calls here)
    addr = _deploy(client, escrow.build(me, seller_id, me).hex())   # buyer=arbiter=wallet

    _call(client, addr, _sel(escrow.FN_DEPOSIT), amount=2)    # buyer deposits 2 BIS
    _call(client, addr, _sel(escrow.FN_DEPOSIT), amount=1)    # +1 BIS
    slots, _ = _slots(addr)
    assert slots.get(escrow.S_AMOUNT) == 3 * UNIT, slots
    assert _bal(addr) == 3 * UNIT

    # RELEASE to the seller's address (pass the wallet's own address as the 28-byte recipient so the
    # payout is observable as a real ledger credit; access is what we're testing, not the payee identity).
    bal_before = client.balance()
    _call(client, addr, _sel(escrow.FN_RELEASE) + wallet)
    slots, _ = _slots(addr)
    assert slots.get(escrow.S_CLOSED) == 1, "escrow should be closed after release"
    assert _bal(addr) == 0, "custody drained to the seller"
    assert client.balance() - bal_before > 2.5, "seller payout (~3 BIS) should land on chain"

    # Closed: a second release reverts (custody stays 0).
    _call(client, addr, _sel(escrow.FN_RELEASE) + wallet)
    assert _bal(addr) == 0


def test_vesting_height_gate(client):
    """Fund a vault unlocking a few blocks in the future; release reverts early, succeeds after unlock."""
    _wait_fork_active(client)
    wallet = client.address
    me = vesting.beneficiary_id_of(wallet)
    unlock = client.block_height() + 8                       # unlock a few blocks ahead
    addr = _deploy(client, vesting.build(me, unlock).hex())

    _call(client, addr, _sel(vesting.FN_FUND), amount=2)     # anyone funds; the wallet funds 2 BIS
    slots, _ = _slots(addr)
    assert slots.get(vesting.S_AMOUNT) == 2 * UNIT, slots

    # Too early: height < unlock -> release reverts, vault still holds the funds.
    if client.block_height() < unlock:
        _call(client, addr, _sel(vesting.FN_RELEASE) + wallet)
        assert _bal(addr) == 2 * UNIT, "release before unlock height must revert"

    # Advance to/after the unlock height, then release succeeds.
    while client.block_height() < unlock:
        client.mine(1)
    bal_before = client.balance()
    _call(client, addr, _sel(vesting.FN_RELEASE) + wallet)
    assert _bal(addr) == 0, "vault should be emptied at/after unlock"
    assert client.balance() - bal_before > 1.5, "beneficiary should receive the ~2 BIS"


def test_token_mint_and_transfer(client):
    """Owner mints the supply, then moves balances between account ids — pure storage accounting."""
    _wait_fork_active(client)
    wallet = client.address
    owner = tok.account_id_of(wallet)
    supply = 1_000_000
    addr = _deploy(client, tok.build(owner, supply).hex())

    _call(client, addr, _sel(tok.FN_INIT))                   # owner-only mint
    slots, _ = _slots(addr)
    assert slots.get(tok.balance_slot(owner)) == supply, "supply minted to owner"

    # transfer 250000 to account 0x000055
    to_acct = 0x55
    cd = _sel(tok.FN_TRANSFER) + to_acct.to_bytes(4, "big").hex() + (250_000).to_bytes(4, "big").hex()
    _call(client, addr, cd)
    slots, _ = _slots(addr)
    assert slots.get(tok.balance_slot(owner)) == 750_000, slots
    assert slots.get(tok.balance_slot(to_acct)) == 250_000, slots

    # a transfer of more than the owner holds reverts (balance unchanged)
    cd_over = _sel(tok.FN_TRANSFER) + to_acct.to_bytes(4, "big").hex() + (10_000_000).to_bytes(4, "big").hex()
    _call(client, addr, cd_over)
    slots, _ = _slots(addr)
    assert slots.get(tok.balance_slot(owner)) == 750_000, "over-balance transfer must revert"

    # double init reverts (supply not re-minted)
    _call(client, addr, _sel(tok.FN_INIT))
    slots, _ = _slots(addr)
    assert slots.get(tok.balance_slot(owner)) == 750_000, "re-init must not re-mint"
