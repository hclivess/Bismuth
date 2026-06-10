"""Raffle demo contract (contracts/raffle.py) — commit-reveal lottery on the postfork VM.

Two layers:
  * OFFLINE adversarial tests drive the assembled bytecode through bismuth_riscv.execute directly (fast,
    no node) — full lifecycle + every rejection path (non-admin draw, wrong seed, double draw, enter
    after draw, non-winner claim, double claim, deterministic winner selection).
  * a LIVE regnet integration test deploys via vm:deploy, enters via vm:call (custody), draws and claims,
    asserting the pot is paid out — gated behind hf2 like the other VM demos.

Run with: python3 -m pytest tests/test_vm_raffle.py -v
"""
import json
import urllib.request
from time import sleep

import pytest

import bismuth_riscv as rv
import raffle
import vm_engine

API = "http://127.0.0.1:3031"
UNIT = 100_000_000


# ----------------------------------------------------------------------------- offline (no node)
def _run(code, sel, caller, callvalue=0, tail=b"", storage=None, balance=0):
    calldata = sel.to_bytes(4, "big") + tail
    return rv.execute(code, calldata=calldata, caller=caller, callvalue=callvalue,
                      storage=dict(storage or {}), block_height=100, self_balance=balance)


def _entered(code, folds_values):
    """Run a sequence of ENTERs, threading storage; return the final storage + pot."""
    st, pot = {}, 0
    for fold, val in folds_values:
        r = _run(code, raffle.FN_ENTER, fold, callvalue=val, storage=st)
        assert r.success
        st, pot = r.storage, pot + val
    return st, pot


SEED = b"\x07" * 32
ADMIN = 0xA0A0A0A0
ENTRANTS = [0x11111111, 0x22222222, 0x33333333]


def _code():
    return raffle.build(ADMIN, raffle.seed_hash_of(SEED))


def test_full_lifecycle_offline():
    code = _code()
    st, pot = _entered(code, [(ENTRANTS[0], 5), (ENTRANTS[1], 3), (ENTRANTS[2], 2)])
    assert st[raffle.S_NUM] == 3 and st[raffle.S_POT] == 10
    widx = raffle.winning_index(SEED, 3)
    wfold = ENTRANTS[widx]
    r = _run(code, raffle.FN_DRAW, ADMIN, tail=SEED, storage=st)
    assert r.success and r.storage[raffle.S_DRAWN] == 1
    assert r.storage[raffle.S_WINNER] == wfold, "on-chain winner must match the off-chain prediction"
    st = r.storage
    recipient = bytes.fromhex("aa" * 28)
    r = _run(code, raffle.FN_CLAIM, wfold, tail=recipient, storage=st, balance=pot)
    assert r.success
    assert r.transfers == [(int.from_bytes(recipient, "big"), pot)], "winner gets the whole pot"


def test_non_admin_cannot_draw():
    code = _code()
    st, _ = _entered(code, [(ENTRANTS[0], 1)])
    assert not _run(code, raffle.FN_DRAW, 0xDEAD, tail=SEED, storage=st).success


def test_wrong_seed_rejected():
    code = _code()
    st, _ = _entered(code, [(ENTRANTS[0], 1)])
    assert not _run(code, raffle.FN_DRAW, ADMIN, tail=b"\x09" * 32, storage=st).success


def test_double_draw_rejected():
    code = _code()
    st, _ = _entered(code, [(ENTRANTS[0], 1), (ENTRANTS[1], 1)])
    st = _run(code, raffle.FN_DRAW, ADMIN, tail=SEED, storage=st).storage
    assert not _run(code, raffle.FN_DRAW, ADMIN, tail=SEED, storage=st).success


def test_enter_after_draw_rejected():
    code = _code()
    st, _ = _entered(code, [(ENTRANTS[0], 1)])
    st = _run(code, raffle.FN_DRAW, ADMIN, tail=SEED, storage=st).storage
    assert not _run(code, raffle.FN_ENTER, ENTRANTS[1], callvalue=1, storage=st).success


def test_enter_requires_value():
    code = _code()
    assert not _run(code, raffle.FN_ENTER, ENTRANTS[0], callvalue=0).success


def test_non_winner_cannot_claim():
    code = _code()
    st, pot = _entered(code, [(ENTRANTS[0], 1), (ENTRANTS[1], 1), (ENTRANTS[2], 1)])
    st = _run(code, raffle.FN_DRAW, ADMIN, tail=SEED, storage=st).storage
    loser = ENTRANTS[(raffle.winning_index(SEED, 3) + 1) % 3]
    assert not _run(code, raffle.FN_CLAIM, loser, tail=b"\xbb" * 28, storage=st, balance=pot).success


def test_double_claim_rejected():
    code = _code()
    st, pot = _entered(code, [(ENTRANTS[0], 1), (ENTRANTS[1], 1)])
    st = _run(code, raffle.FN_DRAW, ADMIN, tail=SEED, storage=st).storage
    wfold = ENTRANTS[raffle.winning_index(SEED, 2)]
    r = _run(code, raffle.FN_CLAIM, wfold, tail=b"\xcc" * 28, storage=st, balance=pot)
    assert r.success
    assert not _run(code, raffle.FN_CLAIM, wfold, tail=b"\xcc" * 28, storage=r.storage, balance=pot).success


def test_winner_selection_varies_with_seed():
    """The committed seed determines the winning index — different seeds can pick different tickets."""
    seen = set()
    for k in range(8):
        seed = bytes([k]) * 32
        code = raffle.build(ADMIN, raffle.seed_hash_of(seed))
        st, _ = _entered(code, [(f, 1) for f in ENTRANTS])
        st = _run(code, raffle.FN_DRAW, ADMIN, tail=seed, storage=st).storage
        widx = raffle.winning_index(seed, 3)
        assert st[raffle.S_WINNER] == ENTRANTS[widx]
        seen.add(widx)
    assert len(seen) >= 2, "selection should not be constant across seeds"


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


def test_raffle_live_regnet(client):
    _wait_fork_active(client)
    seed = b"\x2b" * 32
    admin_id = raffle.party_id_of(client.address)
    addr = _deploy(client, raffle.build(admin_id, raffle.seed_hash_of(seed)).hex())

    # three tickets from this wallet (all share the wallet's CALLER fold -> the wallet is the winner)
    for _ in range(3):
        _call(client, addr, _sel(raffle.FN_ENTER), amount=1)
    slots, bal = _detail(addr)
    assert slots.get(raffle.S_NUM) == 3, slots
    assert slots.get(raffle.S_POT) == 3 * UNIT
    assert bal == 3 * UNIT, "contract holds the 3 BIS pot in custody"

    # admin reveals the seed -> draw
    _call(client, addr, _sel(raffle.FN_DRAW) + seed.hex())
    slots, _ = _detail(addr)
    assert slots.get(raffle.S_DRAWN) == 1, "raffle drawn"
    assert slots.get(raffle.S_WINNER) == admin_id & 0xFFFFFFFF

    # winner claims the whole pot to their transparent address
    bal_before = client.balance()
    _call(client, addr, _sel(raffle.FN_CLAIM) + client.address)
    slots, bal = _detail(addr)
    assert slots.get(raffle.S_CLAIMED) == 1, "claimed"
    assert bal == 0, "custody drained to the winner"
    assert client.balance() - bal_before > 2.5, "winner received ~3 BIS"

    # claiming again reverts (custody stays 0)
    _call(client, addr, _sel(raffle.FN_CLAIM) + client.address)
    assert _detail(addr)[1] == 0
