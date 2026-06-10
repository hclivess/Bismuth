"""VM custody must roll back on a reorg (security-audit regression).

A rollback that undoes the LEDGER but not the VM contract state leaves contract custody balances ahead of
their VM_SINK ledger backing — value the canonical chain never credited becomes spendable across the reorg
(inflation/double-spend of VM custody) — and a stale committed state root wedges consensus. All rollback
paths funnel through chain_ops._rebuild_derived_state (vm_engine.rebuild + state-root recompute); this drives
the regtest_rollback → chain_ops.rollback path and asserts the VM custody + storage are rebuilt to the new
tip. (The live blocknf reorg path uses the SAME helper but needs a two-node harness to drive directly.)

Run with: python3 -m pytest tests/test_vm_rollback.py -v
"""
import json
import urllib.request
from time import sleep

import pytest

pytest.importorskip("lmdb")

import multisig as ms
import vm_engine

API = "http://127.0.0.1:3031"
UNIT = 100_000_000


def _get(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def _wait_fork_active(client):
    for _ in range(25):
        info = _get("/api/vm/contracts")
        assert info["enabled"], "vm not enabled on this node"
        if info.get("fork_height") is not None and client.block_height() > info["fork_height"]:
            return
        client.mine(3)
    raise AssertionError("hf2 fork not active")


def _detail(addr):
    d = _get("/api/vm/contract/" + addr)
    return {int(s["key"]): int(s["value"]) for s in d["storage"]}, int(d["balance"])


def _mine_until(client, predicate, rounds=20):
    for _ in range(rounds):
        if predicate():
            return
        client.mine(2)
        sleep(0.2)
    assert predicate(), "condition not reached after mining"


def test_vm_custody_and_state_root_roll_back(client):
    _wait_fork_active(client)
    me = ms.party_id_of(client.address)

    # deploy a custody vault (multisig contract has a plain FN_DEPOSIT that accumulates the pot)
    before = set(_get("/api/vm/contracts")["contracts"])
    client.send(client.address, 1, "vm:deploy", ms.build([me, 0x1111, 0x2222], 1).hex())
    _mine_until(client, lambda: set(_get("/api/vm/contracts")["contracts"]) - before)
    addr = (set(_get("/api/vm/contracts")["contracts"]) - before).pop()
    keep_tip = client.block_height()                  # the contract exists here; we roll back to this tip

    # deposit 3 BIS -> the contract's VM custody balance and S_POT both become 3 BIS
    client.send(vm_engine.VM_SINK, 3, "vm:call", "%s:%s" % (addr, ms.FN_DEPOSIT.to_bytes(4, "big").hex()))
    _mine_until(client, lambda: _detail(addr)[1] >= 3 * UNIT)
    slots, bal = _detail(addr)
    assert bal == 3 * UNIT and slots.get(ms.S_POT) == 3 * UNIT, "deposit must credit VM custody"

    # ROLL BACK below the deposit (keeping the deploy). The ledger AND the VM state must roll back together
    # via _rebuild_derived_state; otherwise the custody balance survives the reorg = inflation.
    client.rollback(keep_tip + 1)
    sleep(0.4)
    slots, bal = _detail(addr)
    assert bal == 0, "VM custody NOT rolled back on reorg -> custody outruns its ledger backing (inflation)"
    assert slots.get(ms.S_POT, 0) == 0, "VM contract storage NOT rolled back on reorg"

    # and the chain keeps producing (no consensus wedge from a stale state root): mine forward, re-deposit
    client.send(vm_engine.VM_SINK, 2, "vm:call", "%s:%s" % (addr, ms.FN_DEPOSIT.to_bytes(4, "big").hex()))
    _mine_until(client, lambda: _detail(addr)[1] >= 2 * UNIT)
    assert _detail(addr)[1] == 2 * UNIT, "chain wedged after reorg (stale VM state root would reject blocks)"
