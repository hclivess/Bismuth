"""
VM value custody — the BIS actually MOVES (the HTLC piece).

Funds a RISC-V contract through the vm_custody sink, then the contract releases that balance to a fresh
address; asserts the fresh address received the coins and the contract is drained. This is the consensus
value-flow (contract balance in vm_state, settled via sink ledger rows) end-to-end on regnet, plus the
reorg-determinism property.

Run with: python3 -m pytest tests/test_vm_value.py -v
"""
import json
import urllib.request
from time import sleep

import bismuth_riscv as rv
import vm_engine
from bismuth_riscv import addi, asm, ecall

API = "http://127.0.0.1:3031"
SINK = vm_engine.VM_SINK

# PAYER: TRANSFER(calldata[0:28] = recipient, calldata[28:36] = amount). a0 already points at the calldata
# (the recipient), so set a1 = a0+28 (the amount) and ecall SYS_TRANSFER, then HALT.
PAYER = asm(addi(11, 10, 28), addi(17, 0, rv.SYS_TRANSFER), ecall(),
            addi(17, 0, rv.SYS_HALT), ecall())


def _withdraw_calldata(addr_hex, amount_units):
    return addr_hex + "%016x" % amount_units            # 28-byte recipient + 8-byte amount


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


def _deploy_payer(client):
    before = set(_get("/api/vm/contracts")["contracts"])
    client.send(client.address, 1, "vm:deploy", PAYER.hex())
    client.mine(2)
    sleep(0.4)
    return (set(_get("/api/vm/contracts")["contracts"]) - before).pop()


def test_value_moves_through_custody(client):
    _fork_active(client)
    addr = _deploy_payer(client)

    # FUND: 5 BIS sent to the custody sink funds the contract (calldata amount 0 -> no transfer)
    client.send(SINK, 5.0, "vm:call", addr + ":" + "0" * 72)
    client.mine(2)
    sleep(0.4)
    assert int(_get("/api/vm/contract/" + addr)["balance"]) == 500000000, "deposit not custodied"

    # WITHDRAW: the contract releases its 5 BIS to a fresh address
    fresh = "ab" * 28
    client.send(client.address, 0, "vm:call", addr + ":" + _withdraw_calldata(fresh, 500000000))
    client.mine(2)
    sleep(0.4)

    assert int(_get("/api/vm/contract/" + addr)["balance"]) == 0, "contract not drained"
    assert abs(float(_get("/api/balance/" + fresh)["balance"]) - 5.0) < 1e-8, "BIS did not arrive"


def test_custody_is_rebuilt_deterministically_after_reorg(client):
    # The whole reason value custody was hard: a reorg rebuilds vm_state by RE-EXECUTING, so a contract's
    # historical balance must be reproducible. Holding it in vm_state makes it so. Fund, withdraw, then roll
    # the withdraw off and assert the custody balance AND the state root return to the pre-withdraw values.
    _fork_active(client)
    addr = _deploy_payer(client)

    client.send(SINK, 5.0, "vm:call", addr + ":" + "0" * 72)          # fund 5 BIS
    client.mine(2)
    sleep(0.4)
    t_fund = client.block_height()
    assert int(_get("/api/vm/contract/" + addr)["balance"]) == 500000000
    root_fund = _get("/api/vm/contracts")["state_root"]                 # the committed root WITH 5 BIS held

    fresh = "cd" * 28
    client.send(client.address, 0, "vm:call", addr + ":" + _withdraw_calldata(fresh, 200000000))
    client.mine(2)
    sleep(0.4)
    assert int(_get("/api/vm/contract/" + addr)["balance"]) == 300000000
    assert abs(float(_get("/api/balance/" + fresh)["balance"]) - 2.0) < 1e-8

    client.rollback(t_fund + 1)                                        # REORG away the withdraw
    sleep(0.4)
    assert int(_get("/api/vm/contract/" + addr)["balance"]) == 500000000, "custody not rebuilt"
    assert abs(float(_get("/api/balance/" + fresh)["balance"])) < 1e-8, "withdraw payout not reverted"
    assert _get("/api/vm/contracts")["state_root"] == root_fund, "state root diverged after reorg"
