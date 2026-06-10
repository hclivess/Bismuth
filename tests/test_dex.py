"""Decentralized exchange (contracts/dex.py) — a BIS<->token limit order book on the in-core RV32I VM.

OFFLINE adversarial tests drive the assembled bytecode through bismuth_riscv.execute with a tiny harness
that threads storage + simulated VM custody across calls (deposits add, payouts subtract), exactly like
vm_engine does on-chain. They cover the full lifecycle (genesis mint, token transfer, SELL+FILL, BUY+FILL,
CANCEL) and every guard (admin-only/once mint, overspend, exact-payment fill, no refill of a closed order,
maker-only cancel, insufficient balance). A LIVE regnet test deploys the DEX and exercises the consensus
custody+payout path end-to-end (deposits credit custody, fills force payouts as ledger rows).

Run with: python3 -m pytest tests/test_dex.py -v
"""
import json
import urllib.request
from time import sleep

import pytest

import bismuth_riscv as rv
import dex
import vm_engine

API = "http://127.0.0.1:3031"
UNIT = 100_000_000
ADMIN, B, C = 0xAAAA0001, 0xBBBB0002, 0xCCCC0003


def addr28(fold):
    """A 28-byte address whose low 32 bits equal `fold` (so CALLER fold and transfer recipient agree)."""
    return bytes(24) + (fold & 0xFFFFFFFF).to_bytes(4, "big")


def be4(x):
    return (x & 0xFFFFFFFF).to_bytes(4, "big")


class Dex:
    """Offline harness: run the DEX bytecode with persistent storage + a tracked custody balance."""

    def __init__(self, admin=ADMIN, supply=1_000_000):
        self.code = dex.build(admin, supply)
        self.st = {}
        self.custody = 0

    def call(self, sel, caller, tail=b"", value=0):
        return rv.execute(self.code, calldata=be4(sel) + tail, caller=caller, callvalue=value,
                          storage=dict(self.st), block_height=10, self_balance=self.custody + value)

    def ok(self, sel, caller, tail=b"", value=0):
        r = self.call(sel, caller, tail, value)
        assert r.success, "expected success, got revert"
        self.st = r.storage
        self.custody += value
        for _to, amt in r.transfers:
            self.custody -= amt
        return r

    def reverts(self, sel, caller, tail=b"", value=0):
        assert not self.call(sel, caller, tail, value).success, "expected revert"

    def bal(self, fold):
        return self.st.get(dex.balance_key(fold), 0)

    def order(self, oid, field):
        return self.st.get(dex.order_key(oid, field), 0)


def _oid(r):
    return int.from_bytes(r.output, "big")


# ----------------------------------------------------------------------------- offline (no node)
def test_genesis_mint_admin_only_once():
    d = Dex()
    d.reverts(dex.FN_INIT, B)                       # non-admin cannot mint
    d.ok(dex.FN_INIT, ADMIN)
    assert d.bal(ADMIN) == 1_000_000
    d.reverts(dex.FN_INIT, ADMIN)                   # cannot mint twice


def test_token_transfer_and_overspend():
    d = Dex(); d.ok(dex.FN_INIT, ADMIN)
    d.ok(dex.FN_TRANSFER, ADMIN, addr28(B) + be4(400))
    assert d.bal(B) == 400 and d.bal(ADMIN) == 999_600
    d.reverts(dex.FN_TRANSFER, B, addr28(C) + be4(401))   # B only has 400
    d.reverts(dex.FN_TRANSFER, B, addr28(C) + be4(0))     # zero transfer rejected


def test_sell_then_fill_settles_atomically():
    d = Dex(); d.ok(dex.FN_INIT, ADMIN)
    d.ok(dex.FN_TRANSFER, ADMIN, addr28(B) + be4(400))
    r = d.ok(dex.FN_SELL, B, addr28(B) + be4(100) + be4(5000))
    oid = _oid(r)
    assert d.order(oid, dex.ORD_STATUS) == dex.ST_OPEN and d.order(oid, dex.ORD_SIDE) == dex.SIDE_SELL
    assert d.bal(B) == 300, "100 token escrowed by the sell"
    # C fills with exactly 5000 BIS units -> C gets 100 token, B gets the 5000 BIS
    r = d.ok(dex.FN_FILL, C, be4(oid) + addr28(C), value=5000)
    assert d.bal(C) == 100 and d.order(oid, dex.ORD_STATUS) == dex.ST_FILLED
    assert (int.from_bytes(addr28(B), "big"), 5000) in r.transfers
    assert d.custody == 0, "BIS flowed through custody to the maker"


def test_buy_then_fill_settles_atomically():
    d = Dex(); d.ok(dex.FN_INIT, ADMIN)
    d.ok(dex.FN_TRANSFER, ADMIN, addr28(B) + be4(400))    # B holds token to sell into the buy
    # C posts a BUY: wants 60 token, locks 4000 BIS (attached as value)
    r = d.ok(dex.FN_BUY, C, addr28(C) + be4(60) + be4(4000), value=4000)
    boid = _oid(r)
    assert d.order(boid, dex.ORD_SIDE) == dex.SIDE_BUY and d.custody == 4000
    # B fills it: B spends 60 token -> B gets the 4000 BIS, C gets 60 token
    r = d.ok(dex.FN_FILL, B, be4(boid) + addr28(B))
    assert d.bal(C) == 60 and d.bal(B) == 340
    assert (int.from_bytes(addr28(B), "big"), 4000) in r.transfers
    assert d.custody == 0


def test_buy_requires_exact_escrow():
    d = Dex(); d.ok(dex.FN_INIT, ADMIN)
    d.reverts(dex.FN_BUY, C, addr28(C) + be4(60) + be4(4000), value=3999)   # under
    d.reverts(dex.FN_BUY, C, addr28(C) + be4(60) + be4(4000), value=4001)   # over


def test_fill_requires_exact_payment_and_open():
    d = Dex(); d.ok(dex.FN_INIT, ADMIN)
    d.ok(dex.FN_TRANSFER, ADMIN, addr28(B) + be4(400))
    oid = _oid(d.ok(dex.FN_SELL, B, addr28(B) + be4(100) + be4(5000)))
    d.reverts(dex.FN_FILL, C, be4(oid) + addr28(C), value=4999)   # underpay
    d.reverts(dex.FN_FILL, C, be4(oid) + addr28(C), value=5001)   # overpay
    d.ok(dex.FN_FILL, C, be4(oid) + addr28(C), value=5000)        # exact -> fills
    d.reverts(dex.FN_FILL, C, be4(oid) + addr28(C), value=5000)   # cannot re-fill a closed order


def test_sell_requires_balance_and_binds_maker_to_caller():
    d = Dex(); d.ok(dex.FN_INIT, ADMIN)
    d.ok(dex.FN_TRANSFER, ADMIN, addr28(B) + be4(50))
    d.reverts(dex.FN_SELL, B, addr28(B) + be4(100) + be4(10))     # B only has 50 token
    d.reverts(dex.FN_SELL, B, addr28(C) + be4(10) + be4(10))      # maker(28) must be the caller


def test_cancel_refunds_escrow_and_is_maker_only():
    d = Dex(); d.ok(dex.FN_INIT, ADMIN)
    d.ok(dex.FN_TRANSFER, ADMIN, addr28(B) + be4(400))
    # SELL then cancel -> token refunded
    soid = _oid(d.ok(dex.FN_SELL, B, addr28(B) + be4(70) + be4(999)))
    assert d.bal(B) == 330
    d.reverts(dex.FN_CANCEL, C, be4(soid))                        # only the maker may cancel
    d.ok(dex.FN_CANCEL, B, be4(soid))
    assert d.bal(B) == 400 and d.order(soid, dex.ORD_STATUS) == dex.ST_CANCELLED
    d.reverts(dex.FN_FILL, C, be4(soid) + addr28(C), value=999)   # cannot fill a cancelled order
    # BUY then cancel -> BIS refunded to the maker
    boid = _oid(d.ok(dex.FN_BUY, C, addr28(C) + be4(9) + be4(1234), value=1234))
    assert d.custody == 1234
    r = d.ok(dex.FN_CANCEL, C, be4(boid))
    assert (int.from_bytes(addr28(C), "big"), 1234) in r.transfers and d.custody == 0


def test_build_validates_supply():
    with pytest.raises(ValueError):
        dex.build(ADMIN, 2 ** 32)                                # supply must fit a 32-bit word
    with pytest.raises(ValueError):
        dex.build(ADMIN, -1)


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
    client.mine(2); sleep(0.3)
    new = set(_get("/api/vm/contracts")["contracts"]) - before
    assert new, "deploy mined but VM did not register the contract"
    return new.pop()


def _call(client, addr, calldata_hex, amount=0):
    recipient = vm_engine.VM_SINK if amount else client.address
    client.send(recipient, amount, "vm:call", "%s:%s" % (addr, calldata_hex))
    client.mine(1); sleep(0.2)


def _detail(addr):
    d = _get("/api/vm/contract/" + addr)
    return {int(s["key"]): int(s["value"]) for s in d["storage"]}, int(d["balance"])


def _hx(b):
    return b.hex()


def test_dex_live_regnet(client):
    _wait_fork_active(client)
    me = dex.party_id_of(client.address)
    my28 = bytes.fromhex(client.address)          # the RSA wallet IS a 56-hex == 28-byte address
    addr = _deploy(client, dex.build(me, 1_000_000).hex())

    # genesis-mint the supply to the wallet
    _call(client, addr, _hx(be4(dex.FN_INIT)))
    slots, _ = _detail(addr)
    assert slots.get(dex.balance_key(me)) == 1_000_000, "genesis mint did not credit the admin"

    # SELL 200 token for 2 BIS, then FILL it (self-trade: proves escrow -> credit + custody -> payout).
    # The order price is stored in atomic UNITS (the contract compares it to callvalue, which is in units);
    # LiteClient.send amounts are in BIS, so a 2-BIS send arrives as callvalue == 2*UNIT == the order price.
    price_units = 2 * UNIT
    _call(client, addr, _hx(be4(dex.FN_SELL) + my28 + be4(200) + be4(price_units)))
    slots, _ = _detail(addr)
    assert slots.get(dex.balance_key(me)) == 999_800, "sell did not escrow the token"
    oid = slots.get(dex.S_NEXT_OID, 0) - 1        # the order id is the next-counter minus 1
    assert slots.get(dex.order_key(oid, dex.ORD_STATUS)) == dex.ST_OPEN

    _call(client, addr, _hx(be4(dex.FN_FILL) + be4(oid) + my28), amount=2)   # 2 BIS -> callvalue 2*UNIT
    slots, bal = _detail(addr)
    assert slots.get(dex.order_key(oid, dex.ORD_STATUS)) == dex.ST_FILLED, "fill did not close the order"
    assert slots.get(dex.balance_key(me)) == 1_000_000, "token returned to the (self) taker"
    assert bal == 0, "the BIS paid in was forced back out to the maker (custody drained)"

    # BUY 50 token for 1 BIS (lock BIS), then CANCEL it -> the escrowed BIS is refunded (custody drains)
    _call(client, addr, _hx(be4(dex.FN_BUY) + my28 + be4(50) + be4(1 * UNIT)), amount=1)
    slots, bal = _detail(addr)
    boid = slots.get(dex.S_NEXT_OID, 0) - 1
    assert slots.get(dex.order_key(boid, dex.ORD_SIDE)) == dex.SIDE_BUY and bal == 1 * UNIT
    _call(client, addr, _hx(be4(dex.FN_CANCEL) + be4(boid)))
    slots, bal = _detail(addr)
    assert slots.get(dex.order_key(boid, dex.ORD_STATUS)) == dex.ST_CANCELLED
    assert bal == 0, "cancel refunded the escrowed BIS (custody drained)"
