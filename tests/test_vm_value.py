"""
VM value custody — the BIS actually MOVES (the HTLC piece).

Funds a contract through the vm_custody sink, then the contract releases that balance to a fresh address;
asserts the fresh address received the coins and the contract is drained. This is the consensus value-flow
(contract balance in vm_state, settled via sink ledger rows) end-to-end on regnet.

Run with: python3 -m pytest tests/test_vm_value.py -v
"""
import json
import urllib.request
from time import sleep

import bismuth_vm as vm
import vm_engine
from bismuth_vm import push

API = "http://127.0.0.1:3031"
SINK = vm_engine.VM_SINK

# transfers calldata[0:32] (amount) to calldata[32:64] (recipient): push to, push amount, TRANSFER
PAYER = push(32) + bytes([vm.CALLDATALOAD]) + push(0) + bytes([vm.CALLDATALOAD, vm.TRANSFER, vm.STOP])


def _get(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def _fork_active(client):
    for _ in range(25):
        info = _get("/api/vm/contracts")
        if info.get("fork_height") is not None and client.block_height() > info["fork_height"]:
            return
        client.mine(3)
    raise AssertionError("hf2 fork not active")


def test_value_moves_through_custody(client):
    _fork_active(client)
    before = set(_get("/api/vm/contracts")["contracts"])
    client.send(client.address, 1, "vm:deploy", PAYER.hex())
    client.mine(2)
    sleep(0.4)
    addr = (set(_get("/api/vm/contracts")["contracts"]) - before).pop()

    # FUND: 5 BIS sent to the custody sink funds the contract (calldata amount 0 -> no transfer)
    client.send(SINK, 5.0, "vm:call", addr + ":" + "0" * 128)
    client.mine(2)
    sleep(0.4)
    assert int(_get("/api/vm/contract/" + addr)["balance"]) == 500000000, "deposit not custodied"

    # WITHDRAW: the contract releases its 5 BIS to a fresh address
    fresh = "ab" * 28
    calldata = "%064x" % 500000000 + "%064x" % int(fresh, 16)
    client.send(client.address, 0, "vm:call", addr + ":" + calldata)
    client.mine(2)
    sleep(0.4)

    assert int(_get("/api/vm/contract/" + addr)["balance"]) == 0, "contract not drained"
    assert abs(float(_get("/api/balance/" + fresh)["balance"]) - 5.0) < 1e-8, "BIS did not arrive"


def test_custody_is_rebuilt_deterministically_after_reorg(client):
    # The whole reason value custody was hard: a reorg rebuilds vm_state by RE-EXECUTING, so a contract's
    # historical balance must be reproducible. Holding it in vm_state makes it so. Fund, withdraw, then roll
    # the withdraw off and assert the custody balance AND the state root return to the pre-withdraw values.
    _fork_active(client)
    before = set(_get("/api/vm/contracts")["contracts"])
    client.send(client.address, 1, "vm:deploy", PAYER.hex())
    client.mine(2)
    sleep(0.4)
    addr = (set(_get("/api/vm/contracts")["contracts"]) - before).pop()

    client.send(SINK, 5.0, "vm:call", addr + ":" + "0" * 128)          # fund 5 BIS
    client.mine(2)
    sleep(0.4)
    t_fund = client.block_height()
    assert int(_get("/api/vm/contract/" + addr)["balance"]) == 500000000
    root_fund = _get("/api/vm/contracts")["state_root"]                 # the committed root WITH 5 BIS held

    fresh = "cd" * 28
    calldata = "%064x" % 200000000 + "%064x" % int(fresh, 16)          # withdraw 2 BIS
    client.send(client.address, 0, "vm:call", addr + ":" + calldata)
    client.mine(2)
    sleep(0.4)
    assert int(_get("/api/vm/contract/" + addr)["balance"]) == 300000000
    assert abs(float(_get("/api/balance/" + fresh)["balance"]) - 2.0) < 1e-8

    client.rollback(t_fund + 1)                                        # REORG away the withdraw
    sleep(0.4)
    assert int(_get("/api/vm/contract/" + addr)["balance"]) == 500000000, "custody not rebuilt"
    assert abs(float(_get("/api/balance/" + fresh)["balance"])) < 1e-8, "withdraw payout not reverted"
    assert _get("/api/vm/contracts")["state_root"] == root_fund, "state root diverged after reorg"
