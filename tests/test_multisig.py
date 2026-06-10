"""M-of-N multisig vault (contracts/multisig.py) — postfork VM custody released on M owner approvals.

OFFLINE adversarial tests drive the assembled bytecode through bismuth_riscv.execute (no node): full
2-of-3 lifecycle + every rejection path (non-owner propose/approve, below-threshold execute, double
execute, approval reset on a new proposal, stale approvals not counting, amount > pot). A LIVE regnet
test deploys a vault with the wallet as an owner, deposits, proposes and executes a payout (threshold
met by the live owner) — proving the custody+payout path end-to-end.

Run with: python3 -m pytest tests/test_multisig.py -v
"""
import json
import urllib.request
from time import sleep

import bismuth_riscv as rv
import multisig as ms
import vm_engine

API = "http://127.0.0.1:3031"
UNIT = 100_000_000
OWNERS = [0x1111, 0x2222, 0x3333]
RCPT = bytes.fromhex("aa" * 28)


# ----------------------------------------------------------------------------- offline (no node)
def _run(code, sel, caller, callvalue=0, tail=b"", storage=None, balance=0):
    return rv.execute(code, calldata=sel.to_bytes(4, "big") + tail, caller=caller, callvalue=callvalue,
                      storage=dict(storage or {}), block_height=50, self_balance=balance)


def _ptail(rcpt, amt):
    return rcpt + amt.to_bytes(4, "big")


def test_2of3_full_lifecycle():
    code = ms.build(OWNERS, 2)
    st = _run(code, ms.FN_DEPOSIT, 0x9999, callvalue=10).storage
    assert st[ms.S_POT] == 10
    st = _run(code, ms.FN_PROPOSE, OWNERS[0], tail=_ptail(RCPT, 6), storage=st).storage
    assert st[ms.S_PROPOSED] == 1 and st[ms.S_AMT] == 6 and st[ms.TAG_APPROVE | 0] == 1
    assert not _run(code, ms.FN_EXECUTE, 0x9999, storage=st, balance=10).success   # 1 < 2
    st = _run(code, ms.FN_APPROVE, OWNERS[1], storage=st).storage
    r = _run(code, ms.FN_EXECUTE, 0x9999, storage=st, balance=10)
    assert r.success and r.transfers == [(int.from_bytes(RCPT, "big"), 6)]
    assert r.storage[ms.S_EXECUTED] == 1 and r.storage[ms.S_POT] == 4


def test_non_owner_cannot_propose():
    code = ms.build(OWNERS, 2)
    st = _run(code, ms.FN_DEPOSIT, 0x9999, callvalue=10).storage
    assert not _run(code, ms.FN_PROPOSE, 0xDEAD, tail=_ptail(RCPT, 6), storage=st).success


def test_non_owner_cannot_approve():
    code = ms.build(OWNERS, 2)
    st = _run(code, ms.FN_DEPOSIT, 0x9999, callvalue=10).storage
    st = _run(code, ms.FN_PROPOSE, OWNERS[0], tail=_ptail(RCPT, 6), storage=st).storage
    assert not _run(code, ms.FN_APPROVE, 0xDEAD, storage=st).success


def test_execute_below_threshold_rejected():
    code = ms.build(OWNERS, 3)                     # 3-of-3
    st = _run(code, ms.FN_DEPOSIT, 0x9999, callvalue=10).storage
    st = _run(code, ms.FN_PROPOSE, OWNERS[0], tail=_ptail(RCPT, 6), storage=st).storage
    st = _run(code, ms.FN_APPROVE, OWNERS[1], storage=st).storage   # 2 of 3
    assert not _run(code, ms.FN_EXECUTE, 0x9999, storage=st, balance=10).success


def test_double_execute_rejected():
    code = ms.build(OWNERS, 2)
    st = _run(code, ms.FN_DEPOSIT, 0x9999, callvalue=10).storage
    st = _run(code, ms.FN_PROPOSE, OWNERS[0], tail=_ptail(RCPT, 6), storage=st).storage
    st = _run(code, ms.FN_APPROVE, OWNERS[1], storage=st).storage
    st = _run(code, ms.FN_EXECUTE, 0x9999, storage=st, balance=10).storage
    assert not _run(code, ms.FN_EXECUTE, 0x9999, storage=st, balance=4).success


def test_new_proposal_resets_approvals():
    code = ms.build(OWNERS, 2)
    st = _run(code, ms.FN_DEPOSIT, 0x9999, callvalue=10).storage
    st = _run(code, ms.FN_PROPOSE, OWNERS[0], tail=_ptail(RCPT, 6), storage=st).storage
    st = _run(code, ms.FN_APPROVE, OWNERS[1], storage=st).storage   # would be enough...
    st = _run(code, ms.FN_PROPOSE, OWNERS[0], tail=_ptail(RCPT, 3), storage=st).storage  # ...but reset
    assert st[ms.TAG_APPROVE | 1] == 0
    assert not _run(code, ms.FN_EXECUTE, 0x9999, storage=st, balance=10).success   # only proposer approved


def test_approve_is_idempotent():
    code = ms.build(OWNERS, 2)
    st = _run(code, ms.FN_DEPOSIT, 0x9999, callvalue=10).storage
    st = _run(code, ms.FN_PROPOSE, OWNERS[0], tail=_ptail(RCPT, 6), storage=st).storage
    st = _run(code, ms.FN_APPROVE, OWNERS[1], storage=st).storage
    st = _run(code, ms.FN_APPROVE, OWNERS[1], storage=st).storage   # same owner again
    # still exactly two distinct approvers -> executes once
    r = _run(code, ms.FN_EXECUTE, 0x9999, storage=st, balance=10)
    assert r.success and r.transfers == [(int.from_bytes(RCPT, "big"), 6)]


def test_amount_over_pot_rejected():
    code = ms.build(OWNERS, 2)
    st = _run(code, ms.FN_DEPOSIT, 0x9999, callvalue=4).storage
    st = _run(code, ms.FN_PROPOSE, OWNERS[0], tail=_ptail(RCPT, 999), storage=st).storage
    st = _run(code, ms.FN_APPROVE, OWNERS[1], storage=st).storage
    assert not _run(code, ms.FN_EXECUTE, 0x9999, storage=st, balance=4).success


def test_build_validates_params():
    import pytest
    with pytest.raises(ValueError):
        ms.build(OWNERS, 0)                        # threshold < 1
    with pytest.raises(ValueError):
        ms.build(OWNERS, 4)                        # threshold > N
    with pytest.raises(ValueError):
        ms.build([0x1, 0x1], 1)                    # duplicate owners


# ----------------------------------------------------------------------------- live regnet
def _get(path):
    with urllib.request.urlopen(API + path, timeout=8) as r:
        return json.load(r)


def _wait_fork_active(client):
    info = _get("/api/vm/contracts")
    assert info["enabled"], "vm not enabled on this node"
    for _ in range(25):
        info = _get("/api/vm/contracts")
        if info.get("fork_height") is not None and client.block_height() > info["fork_height"]:
            return
        client.mine(3)
    raise AssertionError("hf2 fork not active")


def _deploy(client, code_hex):
    before = set(_get("/api/vm/contracts")["contracts"])
    client.send(client.address, 1, "vm:deploy", code_hex)
    client.mine(2)
    sleep(0.3)
    new = set(_get("/api/vm/contracts")["contracts"]) - before
    assert new, "deploy mined but VM did not register the contract"
    return new.pop()


def _call(client, addr, calldata_hex, amount=0):
    recipient = vm_engine.VM_SINK if amount else client.address
    client.send(recipient, amount, "vm:call", "%s:%s" % (addr, calldata_hex))
    client.mine(1)
    sleep(0.2)


def _detail(addr):
    d = _get("/api/vm/contract/" + addr)
    return {int(s["key"]): int(s["value"]) for s in d["storage"]}, int(d["balance"])


def _sel(n):
    return n.to_bytes(4, "big").hex()


def test_multisig_live_regnet(client):
    _wait_fork_active(client)
    # vault with the live wallet as an owner; threshold 1 so the wallet alone can propose+execute
    me = ms.party_id_of(client.address)
    addr = _deploy(client, ms.build([me, 0x1111, 0x2222], 1).hex())

    _call(client, addr, _sel(ms.FN_DEPOSIT), amount=3)         # fund 3 BIS into the vault
    slots, bal = _detail(addr)
    assert slots.get(ms.S_POT) == 3 * UNIT and bal == 3 * UNIT, slots

    # propose paying 3 BIS to the wallet, then execute (proposer auto-approves -> 1 >= threshold 1)
    amt_hex = (3 * UNIT).to_bytes(4, "big").hex()
    _call(client, addr, _sel(ms.FN_PROPOSE) + client.address + amt_hex)
    slots, _ = _detail(addr)
    assert slots.get(ms.S_PROPOSED) == 1

    bal_before = client.balance()
    _call(client, addr, _sel(ms.FN_EXECUTE))
    slots, bal = _detail(addr)
    assert slots.get(ms.S_EXECUTED) == 1, "executed"
    assert bal == 0, "vault custody drained to the payee"
    assert client.balance() - bal_before > 2.5, "payee received ~3 BIS"

    # executing again reverts (custody stays 0)
    _call(client, addr, _sel(ms.FN_EXECUTE))
    assert _detail(addr)[1] == 0
