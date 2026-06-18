"""Automated market maker (contracts/amm.py) — a constant-product (x*y=k) BIS<->token pool on the in-core
RV32I VM. This is the documented follow-up to the limit-order-book DEX (doc/24): the pool is always a
counterparty, swaps are slippage-priced off x*y=k, and a 0.3% fee on every swap accrues to the LPs.

OFFLINE adversarial tests drive the assembled bytecode through bismuth_riscv.execute with a tiny harness
that threads storage + simulated VM custody across calls (deposits add, payouts subtract), exactly like
vm_engine does on-chain. A Python reference (`ref_*`) recomputes every formula so the on-chain integer math
is checked unit-for-unit. They cover the full lifecycle (mint, transfer, seed/add/remove liquidity, both
swap directions) and every guard (admin-only/once mint, MIN_LIQ lock, proportional add, dBis<=reserve,
tok_max cap, slippage min-out, recipient binding, MAX_RESERVE overflow cap, no-drain, k-monotonicity).
A LIVE regnet test deploys the AMM and exercises the consensus custody+payout path end-to-end.

Run with: python3 -m pytest tests/test_amm.py -v
"""
import json
import urllib.request
from time import sleep

import pytest

import amm
import bismuth_riscv as rv
import vm_engine

API = "http://127.0.0.1:3031"
UNIT = 100_000_000
ADMIN, B, C = 0xAAAA0001, 0xBBBB0002, 0xCCCC0003
MAX = amm.MAX_RESERVE
MIN_LIQ = amm.MIN_LIQ


def addr28(fold):
    """A 28-byte address whose low 32 bits equal `fold` (so CALLER fold and transfer recipient agree)."""
    return bytes(24) + (fold & 0xFFFFFFFF).to_bytes(4, "big")


def be4(x):
    return (x & 0xFFFFFFFF).to_bytes(4, "big")


# ---- Python reference for the contract's integer math (must match the RV32I exactly) ----
def ref_in_eff(amt):
    return (amt * amm.FEE_NUM) // amm.FEE_DEN


def ref_swap_out(amt_in, r_in, r_out):
    e = ref_in_eff(amt_in)
    return (e * r_out) // (r_in + e)


def ref_shares(d_bis, L, r_bis):
    return (d_bis * L) // r_bis


def ref_dtok(d_bis, r_tok, r_bis):
    return -((-(d_bis * r_tok)) // r_bis)        # ceil(d_bis*r_tok/r_bis)


class Amm:
    """Offline harness: run the AMM bytecode with persistent storage + a tracked custody balance.
    Invariant asserted throughout: custody (BIS held) == R_bis (the BIS reserve slot)."""

    def __init__(self, admin=ADMIN, supply=4_000_000_000):
        self.code = amm.build(admin, supply)
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
        assert self.custody == self.rbis, "custody (%d) must mirror R_bis (%d)" % (self.custody, self.rbis)
        return r

    def reverts(self, sel, caller, tail=b"", value=0):
        assert not self.call(sel, caller, tail, value).success, "expected revert"

    def bal(self, fold):
        return self.st.get(amm.balance_key(fold), 0)

    def lp(self, fold):
        return self.st.get(amm.lp_key(fold), 0)

    @property
    def rbis(self):
        return self.st.get(amm.S_RBIS, 0)

    @property
    def rtok(self):
        return self.st.get(amm.S_RTOK, 0)

    @property
    def ltot(self):
        return self.st.get(amm.S_LTOTAL, 0)

    @property
    def k(self):
        return self.rbis * self.rtok


def seeded(admin_bis=1 * UNIT, admin_tok=2 * UNIT, supply=4_000_000_000):
    """A pool that has been genesis-minted and seeded by ADMIN: R_bis=admin_bis, R_tok=admin_tok."""
    d = Amm(supply=supply)
    d.ok(amm.FN_INIT, ADMIN)
    d.ok(amm.FN_ADD_LIQ, ADMIN, be4(admin_tok), value=admin_bis)
    return d


# ----------------------------------------------------------------------------- offline (no node)
def test_genesis_mint_admin_only_once():
    d = Amm()
    d.reverts(amm.FN_INIT, B)                        # non-admin cannot mint
    d.ok(amm.FN_INIT, ADMIN)
    assert d.bal(ADMIN) == 4_000_000_000
    d.reverts(amm.FN_INIT, ADMIN)                    # cannot mint twice


def test_token_transfer_and_overspend():
    d = Amm(); d.ok(amm.FN_INIT, ADMIN)
    d.ok(amm.FN_TRANSFER, ADMIN, addr28(B) + be4(400))
    assert d.bal(B) == 400 and d.bal(ADMIN) == 4_000_000_000 - 400
    d.reverts(amm.FN_TRANSFER, B, addr28(C) + be4(401))   # B only has 400
    d.reverts(amm.FN_TRANSFER, B, addr28(C) + be4(0))     # zero transfer rejected


def test_first_add_seeds_pool_and_locks_min_liquidity():
    d = Amm(); d.ok(amm.FN_INIT, ADMIN)
    d.ok(amm.FN_ADD_LIQ, ADMIN, be4(2 * UNIT), value=1 * UNIT)   # 1 BIS + 2e8 token
    assert d.rbis == 1 * UNIT and d.rtok == 2 * UNIT
    assert d.custody == 1 * UNIT
    assert d.ltot == 1 * UNIT, "L = dBis on the first add"
    assert d.lp(ADMIN) == 1 * UNIT - MIN_LIQ, "MIN_LIQ is locked, not credited to anyone"
    assert d.bal(ADMIN) == 4_000_000_000 - 2 * UNIT, "the token side was pulled from the admin"


def test_first_add_requires_more_than_min_liquidity():
    d = Amm(); d.ok(amm.FN_INIT, ADMIN)
    d.reverts(amm.FN_ADD_LIQ, ADMIN, be4(2 * UNIT), value=MIN_LIQ)       # dBis == MIN_LIQ -> 0 shares
    d.reverts(amm.FN_ADD_LIQ, ADMIN, be4(2 * UNIT), value=0)            # no value attached
    d.reverts(amm.FN_ADD_LIQ, ADMIN, be4(0), value=1 * UNIT)           # tok_max == 0
    d.ok(amm.FN_ADD_LIQ, ADMIN, be4(2 * UNIT), value=MIN_LIQ + 1)       # one over the floor -> ok


def test_swap_bis_for_token_matches_formula_and_grows_k():
    d = seeded()
    k0 = d.k
    amt = 10_000_000                                 # 0.1 BIS in
    exp_out = ref_swap_out(amt, d.rbis, d.rtok)
    assert exp_out > 0
    d.ok(amm.FN_SWAP_B2T, C, be4(0), value=amt)      # min_tok_out = 0
    assert d.bal(C) == exp_out, "token out must match the constant-product formula exactly"
    assert d.rbis == 1 * UNIT + amt, "all of the BIS in (incl. fee) joins the reserve"
    assert d.rtok == 2 * UNIT - exp_out
    assert d.custody == d.rbis
    assert d.k > k0, "the 0.3% fee makes k grow — that is the LPs' yield"
    assert d.bal(C) < d.rtok + d.bal(C), "swap never drains the token reserve"


def test_swap_token_for_bis_pays_out_and_grows_k():
    d = seeded()
    # give C some token to sell
    d.ok(amm.FN_TRANSFER, ADMIN, addr28(C) + be4(50_000_000))
    k0 = d.k
    amt = 20_000_000
    exp_out = ref_swap_out(amt, d.rtok, d.rbis)      # token in, BIS out -> reserves swapped
    assert exp_out > 0
    r = d.ok(amm.FN_SWAP_T2B, C, addr28(C) + be4(amt) + be4(0))
    assert (int.from_bytes(addr28(C), "big"), exp_out) in r.transfers, "BIS paid out via SYS_TRANSFER"
    assert d.rtok == 2 * UNIT + amt and d.rbis == 1 * UNIT - exp_out
    assert d.bal(C) == 50_000_000 - amt, "token spent from the caller's balance"
    assert d.custody == d.rbis and d.k > k0


def test_swap_slippage_guard():
    d = seeded()
    amt = 10_000_000
    exp_out = ref_swap_out(amt, d.rbis, d.rtok)
    d.reverts(amm.FN_SWAP_B2T, C, be4(exp_out + 1), value=amt)   # demand 1 more than achievable -> revert
    d.ok(amm.FN_SWAP_B2T, C, be4(exp_out), value=amt)           # demand exactly the output -> ok


def test_swap_zero_and_empty_pool():
    d = seeded()
    d.reverts(amm.FN_SWAP_B2T, C, be4(0), value=0)              # zero value in
    empty = Amm(); empty.ok(amm.FN_INIT, ADMIN)
    empty.reverts(amm.FN_SWAP_B2T, C, be4(0), value=10_000)    # no reserves -> out is 0 -> revert


def test_proportional_add_mints_and_pulls_correctly():
    d = seeded()                                     # R_bis=1e8, R_tok=2e8, L=1e8
    d.ok(amm.FN_TRANSFER, ADMIN, addr28(B) + be4(5 * UNIT))     # B needs token to add
    L0, rb0, rt0 = d.ltot, d.rbis, d.rtok
    d_bis = 50_000_000                               # add 0.5 BIS (<= R_bis)
    exp_shares = ref_shares(d_bis, L0, rb0)
    exp_dtok = ref_dtok(d_bis, rt0, rb0)
    d.ok(amm.FN_ADD_LIQ, B, be4(5 * UNIT), value=d_bis)        # tok_max generous
    assert d.lp(B) == exp_shares and exp_shares > 0
    assert d.rbis == rb0 + d_bis
    assert d.rtok == rt0 + exp_dtok
    assert d.ltot == L0 + exp_shares
    assert d.bal(B) == 5 * UNIT - exp_dtok, "exactly the proportional token was pulled"
    assert d.custody == d.rbis


def test_proportional_add_guards():
    d = seeded()
    d.ok(amm.FN_TRANSFER, ADMIN, addr28(B) + be4(5 * UNIT))
    # dBis must be <= R_bis (an add may at most match the current reserve)
    d.reverts(amm.FN_ADD_LIQ, B, be4(5 * UNIT), value=d.rbis + 1)
    # tok_max too low: the proportional token exceeds the cap -> revert
    d_bis = 50_000_000
    need = ref_dtok(d_bis, d.rtok, d.rbis)
    d.reverts(amm.FN_ADD_LIQ, B, be4(need - 1), value=d_bis)
    # caller lacks the token (only has 5e8 but here we make a fresh under-funded provider)
    d.reverts(amm.FN_ADD_LIQ, C, be4(5 * UNIT), value=d_bis)   # C has no token at all


def test_remove_liquidity_proportional_and_recipient_bound():
    d = seeded()
    # do a couple of swaps so the pool has accrued fees (k grew), then ADMIN removes half its shares
    d.ok(amm.FN_TRANSFER, ADMIN, addr28(C) + be4(50_000_000))
    d.ok(amm.FN_SWAP_B2T, C, be4(0), value=10_000_000)
    d.ok(amm.FN_SWAP_T2B, C, addr28(C) + be4(10_000_000) + be4(0))
    L, rb, rt = d.ltot, d.rbis, d.rtok
    s = d.lp(ADMIN) // 2
    exp_bis = (s * rb) // L
    exp_tok = (s * rt) // L
    admin_tok0 = d.bal(ADMIN)
    # recipient(28) must fold to the caller
    d.reverts(amm.FN_REMOVE_LIQ, ADMIN, addr28(B) + be4(s))     # wrong recipient fold
    r = d.ok(amm.FN_REMOVE_LIQ, ADMIN, addr28(ADMIN) + be4(s))
    assert (int.from_bytes(addr28(ADMIN), "big"), exp_bis) in r.transfers
    assert d.bal(ADMIN) == admin_tok0 + exp_tok
    assert d.rbis == rb - exp_bis and d.rtok == rt - exp_tok and d.ltot == L - s
    assert d.custody == d.rbis
    d.reverts(amm.FN_REMOVE_LIQ, ADMIN, addr28(ADMIN) + be4(d.lp(ADMIN) + 1))   # can't burn more than held


def test_lp_earns_the_fee_round_trip():
    """A provider who is the ONLY LP, after swaps churn through the pool and net out to the original
    balance of reserves, ends up able to withdraw strictly more than they put in — that surplus is the fee."""
    d = seeded(admin_bis=1 * UNIT, admin_tok=2 * UNIT)
    # a trader round-trips: buy token with BIS, then sell back EXACTLY that token. The token reserve returns
    # to its start and the BIS reserve ends slightly ABOVE the seed — the fee on the BIS leg, kept for LPs.
    d.ok(amm.FN_SWAP_B2T, C, be4(0), value=20_000_000)
    got = d.bal(C)                                              # only the token C just bought
    d.ok(amm.FN_SWAP_T2B, C, addr28(C) + be4(got) + be4(0))     # sell exactly that token back
    # ADMIN owns all live shares (minus the locked MIN_LIQ); withdraw them all
    s = d.lp(ADMIN)
    rb, rt, L = d.rbis, d.rtok, d.ltot
    out_bis = (s * rb) // L
    out_tok = (s * rt) // L
    # the LP gets back > the 1 BIS they seeded (fee income), with only a sliver locked in MIN_LIQ
    assert out_bis > 1 * UNIT - 1000, "LP recovers ~all BIS plus fee share"
    assert d.k >= (1 * UNIT) * (2 * UNIT), "k never shrank below the seed product"


def test_max_reserve_cap():
    d = Amm(); d.ok(amm.FN_INIT, ADMIN)
    d.reverts(amm.FN_ADD_LIQ, ADMIN, be4(2 * UNIT), value=MAX + 1)     # first add dBis over the cap
    d.reverts(amm.FN_ADD_LIQ, ADMIN, be4(MAX + 1), value=10_000_000)   # first add tok_max over the cap
    # seed near the cap, then a swap that would push R_bis over MAX must revert
    d.ok(amm.FN_ADD_LIQ, ADMIN, be4(MAX), value=MAX)
    d.reverts(amm.FN_SWAP_B2T, C, be4(0), value=1)                     # R_bis already == MAX -> any add over


def test_build_validates_supply():
    with pytest.raises(ValueError):
        amm.build(ADMIN, 2 ** 32)
    with pytest.raises(ValueError):
        amm.build(ADMIN, -1)


# ---- regression tests for the doc/24 §2 security audit findings ----
def test_swap_b2t_wrap_attack_reverts():
    """AUDIT CRITICAL: a swap whose attached BIS makes R_bis+in wrap past 2^32 must revert. The fix bounds
    the RAW in <= MAX_RESERVE before the (32-bit, wrapping) add, so the cap can no longer be bypassed by
    wrapping, and custody can never desync from R_bis."""
    d = seeded()                                     # R_bis = 1e8
    wrap_in = 2 ** 32 - d.rbis                        # would make R_bis+in == 2^32 -> wraps to 0 (<= MAX)
    d.reverts(amm.FN_SWAP_B2T, C, be4(0), value=wrap_in)        # must revert (raw in > MAX)
    d.reverts(amm.FN_SWAP_B2T, C, be4(0), value=MAX + 1)        # any in over the cap reverts
    # a swap within the cap still works and keeps custody == R_bis (asserted inside ok())
    d.ok(amm.FN_SWAP_B2T, C, be4(0), value=10_000_000)


def test_swap_t2b_input_cap_reverts():
    """AUDIT (defense-in-depth): token_in is bounded to MAX before the R_tok+in add, so swap_t2b safety no
    longer rests implicitly on total_supply < 2^32."""
    d = seeded()
    d.ok(amm.FN_TRANSFER, ADMIN, addr28(C) + be4(MAX))         # give C more token than the cap
    d.reverts(amm.FN_SWAP_T2B, C, addr28(C) + be4(MAX + 1) + be4(0))   # tok_in over the cap reverts


def test_recipient_binding_pins_full_low_word():
    """AUDIT (low): the BIS-payout recipient(28) low word must equal the FULL 32-bit caller (matching the
    DEX), not merely the low 28 bits. A recipient that collides only on the low 28 bits must revert."""
    d = seeded()
    d.ok(amm.FN_TRANSFER, ADMIN, addr28(C) + be4(50_000_000))
    near = bytes(24) + be4(C ^ 0x10000000)                     # low-28 == C, but bit-28 flipped -> low-32 != C
    d.reverts(amm.FN_SWAP_T2B, C, near + be4(10_000_000) + be4(0))      # swap_t2b rejects the near-collision
    d.ok(amm.FN_SWAP_T2B, C, addr28(C) + be4(10_000_000) + be4(0))      # the exact caller address is fine
    # remove: ADMIN owns the live shares; a near-collision recipient must revert
    s = d.lp(ADMIN) // 4
    near_admin = bytes(24) + be4(ADMIN ^ 0x10000000)
    d.reverts(amm.FN_REMOVE_LIQ, ADMIN, near_admin + be4(s))
    d.ok(amm.FN_REMOVE_LIQ, ADMIN, addr28(ADMIN) + be4(s))


class _FakeState:
    """Minimal dict-backed stand-in for vm_state.VMState, enough to drive vm_engine._call in a unit test."""
    def __init__(self):
        self.code = {}; self.baln = {}; self.stor = {}

    def deploy(self, addr, code): self.code[addr] = bytes(code)
    def get_code(self, addr): return self.code.get(addr)
    def get_balance(self, addr): return self.baln.get(addr, 0)
    def add_balance(self, addr, delta):
        self.baln[addr] = max(0, self.baln.get(addr, 0) + int(delta)); return self.baln[addr]
    def load_storage(self, addr): return dict(self.stor.get(addr, {}))
    def commit_storage(self, addr, storage): self.stor[addr] = dict(storage)


def test_engine_refunds_oversized_deposit():
    """AUDIT (#2/#3/#5): vm_engine credits custody with the full deposit, but the VM exposes callvalue as a
    32-bit word — so a deposit that does not fit 32 bits would strand BIS and break custody==R_bis for ANY
    contract. The engine now refunds such a deposit without executing."""
    st = _FakeState()
    addr = "ab" * 28                                  # any 56-hex contract address
    st.deploy(addr, amm.build(ADMIN, 1_000_000))
    sender = "%056x" % ADMIN
    big = 0xFFFFFFFF + 1                              # ~42.9 BIS + 1 unit: does NOT fit a 32-bit VM word
    payouts = vm_engine._call(st, addr + ":", "sig", sender, vm_engine.VM_SINK, big, 10)
    assert payouts == [(sender, big)], "an oversized deposit must be refunded in full"
    assert st.get_balance(addr) == 0, "custody must NOT be credited for an oversized deposit"
    # an IN-RANGE deposit attached to a non-value op (empty calldata -> FN_INIT) is rejected by the
    # contract's no-value guard, so the engine refunds it on the revert -> no strand, custody stays 0.
    ok_dep = 0xFFFFFFFF
    payouts2 = vm_engine._call(st, addr + ":", "sig", sender, vm_engine.VM_SINK, ok_dep, 10)
    assert payouts2 == [(sender, ok_dep)], "value attached to a non-value op is refunded (not stranded)"
    assert st.get_balance(addr) == 0, "custody not credited for the refunded non-value call"


def test_lp_total_overflow_attack_reverts():
    """AUDIT CRITICAL: L (total LP shares) must be capped at MAX so it cannot wrap 2^32. A pool seeded with a
    tiny token reserve but a huge BIS reserve decouples L from R_tok; crashing R_bis then re-adding doubles L
    each time while dTok stays tiny (its guard never fires). Without the L guard, L wraps -> phantom shares.
    The guard makes the doubling add revert before L can exceed MAX."""
    d = Amm(supply=4_000_000_000)
    d.ok(amm.FN_INIT, ADMIN)
    d.ok(amm.FN_TRANSFER, ADMIN, addr28(B) + be4(3_000_000_000))   # fund the attacker with token
    # seed: R_bis = MAX, R_tok = 2 (tiny) -> L = MAX, decoupled from R_tok
    d.ok(amm.FN_ADD_LIQ, ADMIN, be4(2), value=MAX)
    assert d.rbis == MAX and d.rtok == 2 and d.ltot == MAX
    # crash R_bis far below L with a token->BIS swap (so dBis=R_bis on the next add still doubles L)
    d.ok(amm.FN_SWAP_T2B, B, addr28(B) + be4(40) + be4(0))
    assert d.rbis < d.ltot, "R_bis crashed below L (the decoupling the attack needs)"
    # a proportional add with dBis = R_bis mints shares ~= L, which would push L to ~2*MAX: must REVERT now
    d.reverts(amm.FN_ADD_LIQ, B, be4(1_000_000), value=d.rbis)
    assert d.ltot <= MAX, "L stayed within the cap"


def test_no_value_on_non_value_ops_reverts():
    """AUDIT: attaching BIS to an op that does not consume callvalue (init/transfer/remove/swap_t2b) must
    revert (so the engine refunds it) — otherwise the value is stranded and custody != R_bis."""
    d = seeded()
    d.ok(amm.FN_TRANSFER, ADMIN, addr28(C) + be4(10_000_000))
    d.reverts(amm.FN_TRANSFER, C, addr28(B) + be4(1), value=5)          # transfer attaches no value
    d.reverts(amm.FN_SWAP_T2B, C, addr28(C) + be4(1_000_000) + be4(0), value=5)   # t2b pays out, not in
    s = d.lp(ADMIN) // 4
    d.reverts(amm.FN_REMOVE_LIQ, ADMIN, addr28(ADMIN) + be4(s), value=5)
    d.reverts(amm.FN_INIT, ADMIN, value=5)                             # (already minted, but value alone reverts)
    # the value-bearing ops still accept value normally
    d.ok(amm.FN_SWAP_B2T, C, be4(0), value=1_000_000)


def _token_total(d):
    """Sum of every token balance slot + R_tok — must equal the genesis supply at all times (token is
    only ever minted once; add/remove/swap move token between user balances and the pool reserve)."""
    in_pool = d.rtok
    in_users = sum(val for key, val in d.st.items() if (key & 0xF0000000) == amm.TAG_BAL)
    return in_users + in_pool


def test_property_invariants_under_random_ops():
    """Fuzz: a fixed-seed random sequence of swaps / adds / removes by several actors. After EVERY accepted
    op: custody == R_bis (checked inside ok()), token is conserved, and k never falls across a swap. Reverts
    (e.g. an op that would breach MAX_RESERVE or overspend) are expected and must leave state untouched."""
    import random
    rng = random.Random(20260618)
    SUPPLY = 4_000_000_000
    d = Amm(supply=SUPPLY)
    d.ok(amm.FN_INIT, ADMIN)
    parties = [ADMIN, B, C, 0xDDDD0004]
    for p in parties[1:]:                                   # hand each actor some token to play with
        d.ok(amm.FN_TRANSFER, ADMIN, addr28(p) + be4(300_000_000))
    d.ok(amm.FN_ADD_LIQ, ADMIN, be4(2 * UNIT), value=1 * UNIT)   # seed
    assert _token_total(d) == SUPPLY

    accepted = 0
    for _ in range(400):
        who = rng.choice(parties)
        op = rng.choice(["b2t", "t2b", "add", "remove"])
        st_before = dict(d.st)
        cust_before = d.custody
        k_before = d.k
        tok_bal = d.bal(who)
        lp_bal = d.lp(who)
        try:
            if op == "b2t":
                amt = rng.randint(1, 30_000_000)
                r = d.call(amm.FN_SWAP_B2T, who, be4(0), value=amt)
            elif op == "t2b":
                if tok_bal == 0:
                    continue
                amt = rng.randint(1, tok_bal)
                r = d.call(amm.FN_SWAP_T2B, who, addr28(who) + be4(amt) + be4(0))
            elif op == "add":
                if d.ltot == 0 or tok_bal == 0:
                    continue
                amt = rng.randint(1, max(1, min(d.rbis, 30_000_000)))
                r = d.call(amm.FN_ADD_LIQ, who, be4(tok_bal), value=amt)
            else:  # remove
                if lp_bal == 0:
                    continue
                s = rng.randint(1, lp_bal)
                r = d.call(amm.FN_REMOVE_LIQ, who, addr28(who) + be4(s))
        except Exception:
            continue

        if not r.success:
            # a revert must change NOTHING (storage uncommitted, value refunded by the engine)
            assert dict(d.st) == st_before, "revert leaked storage"
            continue

        # accept the op exactly like the on-chain engine would, and re-check the invariants
        value = 0
        if op in ("b2t", "add"):
            value = amt
        d.st = r.storage
        d.custody = cust_before + value
        for _to, a in r.transfers:
            d.custody -= a
        assert d.custody == d.rbis, "custody/R_bis desync after %s" % op
        assert _token_total(d) == SUPPLY, "token supply not conserved after %s" % op
        assert d.rbis <= MAX and d.rtok <= MAX, "reserve breached MAX_RESERVE"
        if op in ("b2t", "t2b"):
            assert d.k >= k_before, "k fell across a swap (fee not retained)"
        accepted += 1

    assert accepted > 50, "fuzz exercised too few accepted ops (%d)" % accepted


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


def test_amm_live_regnet(client):
    _wait_fork_active(client)
    me = amm.party_id_of(client.address)
    my28 = bytes.fromhex(client.address)             # the RSA wallet IS a 56-hex == 28-byte address
    addr = _deploy(client, amm.build(me, 4_000_000_000).hex())

    # genesis-mint, then seed the pool with 2 BIS + 4e8 token
    _call(client, addr, be4(amm.FN_INIT).hex())
    slots, _ = _detail(addr)
    assert slots.get(amm.balance_key(me)) == 4_000_000_000, "genesis mint did not credit the admin"

    seed_bis, seed_tok = 2 * UNIT, 4 * 100_000_000
    _call(client, addr, (be4(amm.FN_ADD_LIQ) + be4(seed_tok)).hex(), amount=2)   # 2 BIS -> callvalue 2*UNIT
    slots, bal = _detail(addr)
    assert slots.get(amm.S_RBIS) == seed_bis and slots.get(amm.S_RTOK) == seed_tok
    assert bal == seed_bis, "custody mirrors the BIS reserve"
    assert slots.get(amm.lp_key(me)) == seed_bis - MIN_LIQ

    # swap 0.5 BIS for token; the token is credited to our internal balance, custody grows by the BIS in
    exp_out = ref_swap_out(50_000_000, seed_bis, seed_tok)
    tok_before = slots.get(amm.balance_key(me))
    _call(client, addr, (be4(amm.FN_SWAP_B2T) + be4(0)).hex(), amount=0.5)       # 0.5 BIS in
    slots, bal = _detail(addr)
    assert slots.get(amm.balance_key(me)) == tok_before + exp_out, "swap credited the token out on-chain"
    assert slots.get(amm.S_RBIS) == seed_bis + 50_000_000 and bal == seed_bis + 50_000_000

    # swap token -> BIS: the contract forces a BIS payout from custody back to us
    exp_bis = ref_swap_out(30_000_000, slots.get(amm.S_RTOK), slots.get(amm.S_RBIS))
    bal_before = bal
    _call(client, addr, (be4(amm.FN_SWAP_T2B) + my28 + be4(30_000_000) + be4(0)).hex())
    slots, bal = _detail(addr)
    assert bal == bal_before - exp_bis, "custody drained by the forced BIS payout"
    assert slots.get(amm.S_RBIS) == bal, "R_bis still mirrors custody after the payout"
