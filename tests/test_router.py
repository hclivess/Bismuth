"""Multi-pool AMM router (contracts/router.py) — one contract hosting many BIS-paired constant-product
pools, with atomic token->token routing (A->BIS->B). The multi-pool generalization of the single-pool AMM.

OFFLINE adversarial tests drive the assembled bytecode through bismuth_riscv.execute with a harness that
threads storage + simulated VM custody, exactly like vm_engine. A Python reference recomputes every formula
(including the two-hop route) unit-for-unit. The load-bearing invariant — custody == SUM of every pool's
R_bis — is asserted after every accepted op. A LIVE regnet test deploys the router and exercises the
consensus custody path: two pools, a BIS<->token swap, and an atomic token->token route.

Run with: python3 -m pytest tests/test_router.py -v
"""
import json
import urllib.request
from time import sleep

import pytest

import bismuth_riscv as rv
import router
import vm_engine

API = "http://127.0.0.1:3031"
UNIT = 100_000_000
ADMIN, B, C = 0xAAAA0001, 0xBBBB0002, 0xCCCC0003
MAX = router.MAX_RESERVE
MIN_LIQ = router.MIN_LIQ


def addr28(fold):
    return bytes(24) + (fold & 0xFFFFFFFF).to_bytes(4, "big")


def be4(x):
    return (x & 0xFFFFFFFF).to_bytes(4, "big")


# ---- Python reference (must match the RV32I exactly) ----
def ref_out(amt_in, r_in, r_out):
    e = (amt_in * router.FEE_NUM) // router.FEE_DEN
    return (e * r_out) // (r_in + e)


def ref_t2t(amt_in, rt_a, rb_a, rb_b, rt_b):
    """token A -> BIS -> token B. Returns (bis_mid, out_b)."""
    bis_mid = ref_out(amt_in, rt_a, rb_a)
    out_b = ref_out(bis_mid, rb_b, rt_b)
    return bis_mid, out_b


class Router:
    """Offline harness: run the router bytecode with persistent storage + tracked custody.
    Invariant asserted in ok(): custody == sum of R_bis over every created pool."""

    def __init__(self, admin=ADMIN):
        self.code = router.build(admin)
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
        assert self.custody == self.sum_rbis, "custody (%d) != sum R_bis (%d)" % (self.custody, self.sum_rbis)
        return r

    def reverts(self, sel, caller, tail=b"", value=0):
        assert not self.call(sel, caller, tail, value).success, "expected revert"

    @property
    def next_tid(self):
        return self.st.get(router.S_NEXT_TID, 0)

    @property
    def sum_rbis(self):
        return sum(self.rbis(t) for t in range(self.next_tid))

    def rbis(self, tid):
        return self.st.get(router.pool_key(tid, router.POOL_RBIS), 0)

    def rtok(self, tid):
        return self.st.get(router.pool_key(tid, router.POOL_RTOK), 0)

    def ltot(self, tid):
        return self.st.get(router.pool_key(tid, router.POOL_L), 0)

    def bal(self, tid, fold):
        return self.st.get(router.balance_key(tid, fold), 0)

    def lp(self, tid, fold):
        return self.st.get(router.lp_key(tid, fold), 0)

    # convenience wrappers
    def create(self, supply, caller=ADMIN):
        r = self.ok(router.FN_CREATE, caller, be4(supply))
        return int.from_bytes(r.output, "big")

    def add(self, tid, tok_max, value, caller):
        return self.ok(router.FN_ADD_LIQ, caller, be4(tid) + be4(tok_max), value=value)

    def seed(self, tid, bis, tok, caller=ADMIN):
        return self.add(tid, tok, bis, caller)


def _supply_total(d, tid):
    """sum of token tid balances + R_tok[tid] — conserved (token minted once on create)."""
    pool = d.rtok(tid)
    users = sum(v for k, v in d.st.items() if (k & 0xF0000000) == router.TAG_BAL and ((k >> 24) & 0xF) == tid)
    return users + pool


# ----------------------------------------------------------------------------- offline (no node)
def test_create_token_admin_only_counter_and_cap():
    d = Router()
    d.reverts(router.FN_CREATE, B, be4(1_000_000))         # non-admin cannot create
    t0 = d.create(1_000_000); t1 = d.create(2_000_000)
    assert t0 == 0 and t1 == 1
    assert d.bal(0, ADMIN) == 1_000_000 and d.bal(1, ADMIN) == 2_000_000
    assert d.next_tid == 2
    # fill up to MAX_TOKENS then the next create reverts
    for _ in range(router.MAX_TOKENS - 2):
        d.create(1000)
    assert d.next_tid == router.MAX_TOKENS
    d.reverts(router.FN_CREATE, ADMIN, be4(1000))


def test_transfer_requires_created_token():
    d = Router(); d.create(1_000_000)
    d.ok(router.FN_TRANSFER, ADMIN, be4(0) + addr28(B) + be4(500))
    assert d.bal(0, B) == 500
    d.reverts(router.FN_TRANSFER, ADMIN, be4(5) + addr28(B) + be4(1))   # token 5 not created


def test_seed_and_single_hop_swaps_per_pool():
    d = Router(); d.create(2_000_000_000)
    d.seed(0, 1 * UNIT, 2 * UNIT)                          # pool 0: 1 BIS / 2e8 token
    assert d.rbis(0) == 1 * UNIT and d.rtok(0) == 2 * UNIT and d.custody == 1 * UNIT
    # BIS -> token
    exp = ref_out(10_000_000, d.rbis(0), d.rtok(0))
    d.ok(router.FN_SWAP_B2T, C, be4(0) + be4(0), value=10_000_000)
    assert d.bal(0, C) == exp and d.rbis(0) == 1 * UNIT + 10_000_000
    # token -> BIS
    d.ok(router.FN_TRANSFER, ADMIN, be4(0) + addr28(C) + be4(20_000_000))  # give C token 0
    exp2 = ref_out(15_000_000, d.rtok(0), d.rbis(0))
    r = d.ok(router.FN_SWAP_T2B, C, be4(0) + addr28(C) + be4(15_000_000) + be4(0))
    assert (int.from_bytes(addr28(C), "big"), exp2) in r.transfers


def test_token_to_token_route_matches_reference_and_conserves():
    d = Router()
    d.create(2_000_000_000); d.create(2_000_000_000)       # tokens 0 and 1
    d.seed(0, 1 * UNIT, 2 * UNIT)                          # pool 0: price 2 tok0/BIS
    d.seed(1, 1 * UNIT, 5 * UNIT)                          # pool 1: price 5 tok1/BIS
    d.ok(router.FN_TRANSFER, ADMIN, be4(0) + addr28(C) + be4(50_000_000))   # give C token 0
    rt_a, rb_a, rb_b, rt_b = d.rtok(0), d.rbis(0), d.rbis(1), d.rtok(1)
    cust0 = d.custody
    amt = 30_000_000
    bis_mid, exp_out = ref_t2t(amt, rt_a, rb_a, rb_b, rt_b)
    assert bis_mid > 0 and exp_out > 0
    d.ok(router.FN_SWAP_T2T, C, be4(0) + be4(1) + be4(amt) + be4(0))
    assert d.bal(1, C) == exp_out, "token-out must match the two-hop reference exactly"
    assert d.bal(0, C) == 50_000_000 - amt, "token-in spent"
    # the intermediate BIS just moved A->B: pool 0 lost it, pool 1 gained it, custody unchanged
    assert d.rbis(0) == rb_a - bis_mid and d.rbis(1) == rb_b + bis_mid
    assert d.custody == cust0, "no BIS entered or left custody on a token->token route"
    assert _supply_total(d, 0) == 2_000_000_000 and _supply_total(d, 1) == 2_000_000_000


def test_t2t_both_pools_earn_fee_k_grows():
    d = Router()
    d.create(2_000_000_000); d.create(2_000_000_000)
    d.seed(0, 1 * UNIT, 2 * UNIT); d.seed(1, 1 * UNIT, 5 * UNIT)
    d.ok(router.FN_TRANSFER, ADMIN, be4(0) + addr28(C) + be4(50_000_000))
    k0a, k0b = d.rbis(0) * d.rtok(0), d.rbis(1) * d.rtok(1)
    d.ok(router.FN_SWAP_T2T, C, be4(0) + be4(1) + be4(20_000_000) + be4(0))
    assert d.rbis(0) * d.rtok(0) > k0a, "pool A (the sell leg) keeps a fee -> its k grows"
    assert d.rbis(1) * d.rtok(1) > k0b, "pool B (the buy leg) keeps a fee -> its k grows"


def test_t2t_guards():
    d = Router()
    d.create(2_000_000_000); d.create(2_000_000_000)
    d.seed(0, 1 * UNIT, 2 * UNIT); d.seed(1, 1 * UNIT, 5 * UNIT)
    d.ok(router.FN_TRANSFER, ADMIN, be4(0) + addr28(C) + be4(50_000_000))
    d.reverts(router.FN_SWAP_T2T, C, be4(0) + be4(0) + be4(10_000_000) + be4(0))   # same token in/out
    d.reverts(router.FN_SWAP_T2T, C, be4(0) + be4(7) + be4(10_000_000) + be4(0))   # token 7 not created
    d.reverts(router.FN_SWAP_T2T, C, be4(0) + be4(1) + be4(MAX + 1) + be4(0))      # amt_in over the cap
    # slippage: demand 1 more than achievable
    bis_mid, exp = ref_t2t(10_000_000, d.rtok(0), d.rbis(0), d.rbis(1), d.rtok(1))
    d.reverts(router.FN_SWAP_T2T, C, be4(0) + be4(1) + be4(10_000_000) + be4(exp + 1))
    d.ok(router.FN_SWAP_T2T, C, be4(0) + be4(1) + be4(10_000_000) + be4(exp))


def test_add_remove_liquidity_per_pool():
    d = Router(); d.create(2_000_000_000)
    d.seed(0, 1 * UNIT, 2 * UNIT)
    d.ok(router.FN_TRANSFER, ADMIN, be4(0) + addr28(B) + be4(5 * UNIT))
    L0, rb0, rt0 = d.ltot(0), d.rbis(0), d.rtok(0)
    d_bis = 50_000_000
    exp_sh = (d_bis * L0) // rb0
    exp_dt = -((-(d_bis * rt0)) // rb0)
    d.add(0, 5 * UNIT, d_bis, B)
    assert d.lp(0, B) == exp_sh and d.rtok(0) == rt0 + exp_dt and d.rbis(0) == rb0 + d_bis
    # remove half of B's shares -> proportional BIS (transfer) + token back
    s = d.lp(0, B) // 2
    L, rb, rt = d.ltot(0), d.rbis(0), d.rtok(0)
    r = d.ok(router.FN_REMOVE_LIQ, B, be4(0) + addr28(B) + be4(s))
    assert (int.from_bytes(addr28(B), "big"), (s * rb) // L) in r.transfers
    assert d.bal(0, B) == 5 * UNIT - exp_dt + (s * rt) // L


def test_recipient_binding_full_low_word():
    d = Router(); d.create(2_000_000_000); d.seed(0, 1 * UNIT, 2 * UNIT)
    d.ok(router.FN_TRANSFER, ADMIN, be4(0) + addr28(C) + be4(20_000_000))
    near = bytes(24) + be4(C ^ 0x10000000)                 # low-24 == C, low-32 != C
    d.reverts(router.FN_SWAP_T2B, C, be4(0) + near + be4(10_000_000) + be4(0))
    d.ok(router.FN_SWAP_T2B, C, be4(0) + addr28(C) + be4(10_000_000) + be4(0))


def test_swap_b2t_wrap_attack_reverts():
    d = Router(); d.create(2_000_000_000); d.seed(0, 1 * UNIT, 2 * UNIT)
    d.reverts(router.FN_SWAP_B2T, C, be4(0) + be4(0), value=2 ** 32 - d.rbis(0))   # would wrap R_bis+in
    d.reverts(router.FN_SWAP_B2T, C, be4(0) + be4(0), value=MAX + 1)


def test_lp_total_overflow_attack_reverts():
    """AUDIT CRITICAL (also fixed in amm.py): a pool seeded with tiny R_tok but huge L lets a proportional
    add double L while dTok stays tiny; without the L<=MAX guard, L wraps 2^32 -> phantom shares + reserve
    corruption on a later remove. The guard makes the doubling add revert."""
    d = Router(); d.create(4_000_000_000)
    d.ok(router.FN_TRANSFER, ADMIN, be4(0) + addr28(B) + be4(3_000_000_000))
    d.add(0, 2, MAX, ADMIN)                                # seed pool 0: R_bis=MAX, R_tok=2, L=MAX
    assert d.rbis(0) == MAX and d.rtok(0) == 2 and d.ltot(0) == MAX
    d.ok(router.FN_SWAP_T2B, B, be4(0) + addr28(B) + be4(40) + be4(0))   # crash R_bis below L
    assert d.rbis(0) < d.ltot(0)
    d.reverts(router.FN_ADD_LIQ, B, be4(0) + be4(1_000_000), value=d.rbis(0))   # would ~double L -> revert
    assert d.ltot(0) <= MAX


def test_no_value_on_non_value_ops_reverts():
    """AUDIT: BIS attached to a non-value selector (create/transfer/remove/swap_t2b/swap_t2t) must revert so
    the engine refunds it (else it is stranded and custody != sum R_bis)."""
    d = Router(); d.create(2_000_000_000); d.create(2_000_000_000)
    d.seed(0, 1 * UNIT, 2 * UNIT); d.seed(1, 1 * UNIT, 5 * UNIT)
    d.ok(router.FN_TRANSFER, ADMIN, be4(0) + addr28(C) + be4(50_000_000))
    d.reverts(router.FN_CREATE, ADMIN, be4(1000), value=5)
    d.reverts(router.FN_TRANSFER, ADMIN, be4(0) + addr28(C) + be4(1), value=5)
    d.reverts(router.FN_SWAP_T2B, C, be4(0) + addr28(C) + be4(1_000_000) + be4(0), value=5)
    d.reverts(router.FN_SWAP_T2T, C, be4(0) + be4(1) + be4(1_000_000) + be4(0), value=5)
    s = d.lp(0, ADMIN) // 4
    d.reverts(router.FN_REMOVE_LIQ, ADMIN, be4(0) + addr28(ADMIN) + be4(s), value=5)
    # value-bearing ops still work
    d.ok(router.FN_SWAP_B2T, C, be4(0) + be4(0), value=1_000_000)


def test_property_invariants_multipool():
    """Random create/add/remove/swap/route across several pools. Custody is tracked INDEPENDENTLY (deposits
    add, payouts subtract) and asserted equal to the sum of every pool's R_bis after each accepted op; each
    token's supply (balances + R_tok) is conserved. A token->token route must move zero net custody."""
    import random
    rng = random.Random(424242)
    d = Router()
    SUP = 2_000_000_000
    ntok = 3
    for _ in range(ntok):
        d.create(SUP)
    parties = [ADMIN, B, C, 0xDDDD0004]
    for t in range(ntok):
        for p in parties[1:]:
            d.ok(router.FN_TRANSFER, ADMIN, be4(t) + addr28(p) + be4(100_000_000))
        d.seed(t, (t + 1) * 50_000_000, (t + 2) * 60_000_000)   # distinct prices per pool

    accepted = 0
    for _ in range(500):
        who = rng.choice(parties)
        op = rng.choice(["b2t", "t2b", "t2t", "add", "remove"])
        tid = rng.randrange(ntok)
        before = dict(d.st)
        cust_before = d.custody
        value = 0
        if op == "b2t":
            value = rng.randint(1, 20_000_000)
            res = d.call(router.FN_SWAP_B2T, who, be4(tid) + be4(0), value=value)
        elif op == "t2b":
            bal = d.bal(tid, who)
            if bal == 0:
                continue
            res = d.call(router.FN_SWAP_T2B, who, be4(tid) + addr28(who) + be4(rng.randint(1, bal)) + be4(0))
        elif op == "t2t":
            tout = rng.randrange(ntok)
            bal = d.bal(tid, who)
            if tout == tid or bal == 0:
                continue
            res = d.call(router.FN_SWAP_T2T, who, be4(tid) + be4(tout) + be4(rng.randint(1, bal)) + be4(0))
        elif op == "add":
            bal = d.bal(tid, who)
            if d.ltot(tid) == 0 or bal == 0:
                continue
            value = rng.randint(1, max(1, min(d.rbis(tid), 20_000_000)))
            res = d.call(router.FN_ADD_LIQ, who, be4(tid) + be4(bal), value=value)
        else:
            lp = d.lp(tid, who)
            if lp == 0:
                continue
            res = d.call(router.FN_REMOVE_LIQ, who, be4(tid) + addr28(who) + be4(rng.randint(1, lp)))

        if not res.success:
            assert dict(d.st) == before, "revert leaked storage"
            continue

        # accept exactly like the engine: deposit adds to custody, payouts subtract
        d.st = res.storage
        d.custody = cust_before + value
        for _to, amt in res.transfers:
            d.custody -= amt
        if op == "t2t":
            assert d.custody == cust_before and not res.transfers, "a token->token route moves no custody"
        assert d.custody == d.sum_rbis, "custody (%d) != sum R_bis (%d) after %s" % (d.custody, d.sum_rbis, op)
        assert all(_supply_total(d, t) == SUP for t in range(ntok)), "token supply not conserved (%s)" % op
        assert all(d.rbis(t) <= MAX and d.rtok(t) <= MAX for t in range(ntok)), "reserve > MAX"
        accepted += 1

    assert accepted > 60, "fuzz exercised too few ops (%d)" % accepted


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


def test_router_live_regnet(client):
    _wait_fork_active(client)
    me = router.party_id_of(client.address)
    my28 = bytes.fromhex(client.address)
    addr = _deploy(client, router.build(me).hex())

    # create two tokens, seed two pools
    _call(client, addr, (be4(router.FN_CREATE) + be4(2_000_000_000)).hex())
    _call(client, addr, (be4(router.FN_CREATE) + be4(2_000_000_000)).hex())
    slots, _ = _detail(addr)
    assert slots.get(router.S_NEXT_TID) == 2
    assert slots.get(router.balance_key(0, me)) == 2_000_000_000

    _call(client, addr, (be4(router.FN_ADD_LIQ) + be4(0) + be4(4 * UNIT)).hex(), amount=2)   # pool0: 2 BIS/4e8
    _call(client, addr, (be4(router.FN_ADD_LIQ) + be4(1) + be4(6 * UNIT)).hex(), amount=2)   # pool1: 2 BIS/6e8
    slots, bal = _detail(addr)
    assert slots.get(router.pool_key(0, router.POOL_RBIS)) == 2 * UNIT
    assert bal == 4 * UNIT, "custody == sum of both pools' R_bis"

    # atomic token0 -> token1 route; the mid BIS stays in custody (sum R_bis unchanged)
    rt_a, rb_a = slots[router.pool_key(0, router.POOL_RTOK)], slots[router.pool_key(0, router.POOL_RBIS)]
    rb_b, rt_b = slots[router.pool_key(1, router.POOL_RBIS)], slots[router.pool_key(1, router.POOL_RTOK)]
    _, exp_out = ref_t2t(50_000_000, rt_a, rb_a, rb_b, rt_b)
    tok1_before = slots.get(router.balance_key(1, me), 0)
    _call(client, addr, (be4(router.FN_SWAP_T2T) + be4(0) + be4(1) + be4(50_000_000) + be4(0)).hex())
    slots, bal2 = _detail(addr)
    assert slots.get(router.balance_key(1, me)) == tok1_before + exp_out, "route credited token1 on-chain"
    assert bal2 == 4 * UNIT, "custody unchanged by a token->token route (mid BIS never left)"
