"""
Demo VM contracts — fast off-chain unit tests (no node).

Executes the hand-authored RISC-V contracts in contracts/ directly through the deterministic interpreter
(bismuth_riscv.execute), with a tiny harness that mirrors vm_engine's custody model (a vm:call to the
sink deposits its value into the contract; a SYS_TRANSFER debits that custody and queues a payout, applied
only on success). This is the exhaustive behaviour/edge-case matrix — the regnet tests prove the same
contracts over real on-chain transactions.

Run with: python3 -m pytest tests/test_contracts_offchain.py -v
"""
import bismuth_riscv as rv
import escrow
import prediction_market as pm
import token_contract as tok
import vesting

GAS = 1_000_000


def be(n, w):
    return int(n).to_bytes(w, "big")


def sel(n):
    return be(n, 4)


class Chain:
    """Minimal stand-in for vm_engine custody: deposit-on-call, TRANSFER-out, revert keeps deposit."""

    def __init__(self, code):
        self.code = code
        self.storage = {}
        self.balance = 0
        self.payouts = []

    def call(self, caller, value, calldata, height=0):
        self.balance += value                     # the ledger already moved the deposit into custody
        r = rv.execute(self.code, calldata=calldata, caller=caller & 0xFFFFFFFF, callvalue=value,
                       storage=dict(self.storage), gas_limit=GAS, block_height=height,
                       self_balance=self.balance)
        if not r.success:
            return False, r                       # revert: deposit stays in custody, storage untouched
        self.storage = r.storage
        for to, amt in r.transfers:
            self.balance -= amt
            self.payouts.append((to, amt))
        return True, r


# ============================ PREDICTION MARKET ============================
RES = 0xAA11BB22
ALICE, BOB, CAROL = 1, 2, 3


def _market():
    c = Chain(pm.build(RES))
    assert c.call(ALICE, 300, sel(pm.FN_BUY_YES))[0]
    assert c.call(BOB, 100, sel(pm.FN_BUY_YES))[0]
    assert c.call(CAROL, 400, sel(pm.FN_BUY_NO))[0]
    return c


def test_pm_buys_accumulate_pots_and_custody():
    c = _market()
    assert c.storage.get(pm.S_YESPOT) == 400
    assert c.storage.get(pm.S_NOPOT) == 400
    assert c.balance == 800


def test_pm_zero_value_buy_reverts():
    c = Chain(pm.build(RES))
    assert not c.call(ALICE, 0, sel(pm.FN_BUY_YES))[0]


def test_pm_unknown_selector_reverts():
    c = _market()
    assert not c.call(ALICE, 0, sel(99))[0]


def test_pm_only_resolver_can_resolve():
    c = _market()
    assert not c.call(ALICE, 0, sel(pm.FN_RESOLVE) + be(1, 4))[0]   # non-resolver
    assert c.storage.get(pm.S_RESOLVED) in (None, 0)
    assert c.call(RES, 0, sel(pm.FN_RESOLVE) + be(1, 4))[0]         # resolver
    assert c.storage.get(pm.S_RESOLVED) == 1 and c.storage.get(pm.S_OUTCOME) == 1


def test_pm_no_double_resolve():
    c = _market()
    assert c.call(RES, 0, sel(pm.FN_RESOLVE) + be(1, 4))[0]
    assert not c.call(RES, 0, sel(pm.FN_RESOLVE) + be(2, 4))[0]
    assert c.storage.get(pm.S_OUTCOME) == 1                          # unchanged


def test_pm_bad_outcome_reverts():
    c = _market()
    assert not c.call(RES, 0, sel(pm.FN_RESOLVE) + be(3, 4))[0]      # outcome must be 1 or 2
    assert c.storage.get(pm.S_RESOLVED) in (None, 0)


def test_pm_claim_before_resolve_reverts():
    c = _market()
    assert not c.call(ALICE, 0, sel(pm.FN_CLAIM) + be(0xA11CE, 28))[0]


def test_pm_prorata_payout_and_claim_once_and_loser_blocked():
    c = _market()
    assert c.call(RES, 0, sel(pm.FN_RESOLVE) + be(1, 4))[0]          # YES wins
    # loser (NO) cannot claim
    assert not c.call(CAROL, 0, sel(pm.FN_CLAIM) + be(0xCABE, 28))[0]
    # Alice: 300 * 800 / 400 = 600
    assert c.call(ALICE, 0, sel(pm.FN_CLAIM) + be(0xA11CE, 28))[0]
    assert (0xA11CE, 600) in c.payouts
    # claim-once
    assert not c.call(ALICE, 0, sel(pm.FN_CLAIM) + be(0xA11CE, 28))[0]
    # Bob: 100 * 800 / 400 = 200
    assert c.call(BOB, 0, sel(pm.FN_CLAIM) + be(0xB0B, 28))[0]
    assert (0xB0B, 200) in c.payouts
    # pot fully distributed
    assert c.balance == 0


def test_pm_resolve_no_side_wins():
    c = _market()
    assert c.call(RES, 0, sel(pm.FN_RESOLVE) + be(2, 4))[0]          # NO wins
    # Carol staked all the NO -> takes the whole 800 pot
    assert c.call(CAROL, 0, sel(pm.FN_CLAIM) + be(0xCABE, 28))[0]
    assert (0xCABE, 800) in c.payouts
    # a YES backer (loser) gets nothing
    assert not c.call(ALICE, 0, sel(pm.FN_CLAIM) + be(0xA11CE, 28))[0]


# ================================ ESCROW ================================
BUYER, SELLER, ARB = 0x111, 0x222, 0x333


def test_escrow_only_buyer_deposits():
    c = Chain(escrow.build(BUYER, SELLER, ARB))
    assert not c.call(SELLER, 500, sel(escrow.FN_DEPOSIT))[0]
    assert c.call(BUYER, 500, sel(escrow.FN_DEPOSIT))[0]
    assert c.storage.get(escrow.S_AMOUNT) == 500


def test_escrow_arbiter_release_to_seller():
    c = Chain(escrow.build(BUYER, SELLER, ARB))
    assert c.call(BUYER, 500, sel(escrow.FN_DEPOSIT))[0]
    assert c.call(BUYER, 300, sel(escrow.FN_DEPOSIT))[0]
    assert not c.call(0x999, 0, sel(escrow.FN_RELEASE) + be(SELLER, 28))[0]   # stranger can't release
    assert c.call(ARB, 0, sel(escrow.FN_RELEASE) + be(SELLER, 28))[0]
    assert c.payouts == [(SELLER, 800)]
    assert c.storage.get(escrow.S_CLOSED) == 1
    assert not c.call(ARB, 0, sel(escrow.FN_RELEASE) + be(SELLER, 28))[0]     # closed


def test_escrow_seller_refund_to_buyer():
    c = Chain(escrow.build(BUYER, SELLER, ARB))
    assert c.call(BUYER, 1000, sel(escrow.FN_DEPOSIT))[0]
    assert not c.call(BUYER, 0, sel(escrow.FN_REFUND) + be(BUYER, 28))[0]     # buyer can't self-refund
    assert c.call(SELLER, 0, sel(escrow.FN_REFUND) + be(BUYER, 28))[0]
    assert c.payouts == [(BUYER, 1000)]


def test_escrow_release_without_funds_reverts():
    c = Chain(escrow.build(BUYER, SELLER, ARB))
    assert not c.call(ARB, 0, sel(escrow.FN_RELEASE) + be(SELLER, 28))[0]


# ================================ VESTING ===============================
BENF, UNLOCK = 0xABCD, 100


def test_vesting_release_blocked_before_unlock():
    c = Chain(vesting.build(BENF, UNLOCK))
    assert c.call(0x1, 700, sel(vesting.FN_FUND), height=10)[0]      # anyone funds
    assert c.storage.get(vesting.S_AMOUNT) == 700
    assert not c.call(BENF, 0, sel(vesting.FN_RELEASE) + be(BENF, 28), height=99)[0]
    assert c.balance == 700


def test_vesting_non_beneficiary_blocked():
    c = Chain(vesting.build(BENF, UNLOCK))
    assert c.call(0x1, 700, sel(vesting.FN_FUND), height=10)[0]
    assert not c.call(0x5, 0, sel(vesting.FN_RELEASE) + be(BENF, 28), height=100)[0]


def test_vesting_release_at_unlock():
    c = Chain(vesting.build(BENF, UNLOCK))
    assert c.call(0x1, 700, sel(vesting.FN_FUND), height=10)[0]
    assert c.call(BENF, 0, sel(vesting.FN_RELEASE) + be(BENF, 28), height=100)[0]
    assert c.payouts == [(BENF, 700)]
    assert not c.call(BENF, 0, sel(vesting.FN_RELEASE) + be(BENF, 28), height=120)[0]   # empty


# ================================= TOKEN ================================
OWNER, SUPPLY = 0xDEAD, 1_000_000


def test_token_init_owner_only_and_once():
    c = Chain(tok.build(OWNER, SUPPLY))
    assert not c.call(0x1, 0, sel(tok.FN_INIT))[0]                   # non-owner
    assert c.call(OWNER, 0, sel(tok.FN_INIT))[0]
    assert c.storage.get(tok.balance_slot(OWNER)) == SUPPLY
    assert not c.call(OWNER, 0, sel(tok.FN_INIT))[0]                 # once


def test_token_transfer_moves_balance():
    c = Chain(tok.build(OWNER, SUPPLY))
    assert c.call(OWNER, 0, sel(tok.FN_INIT))[0]
    assert c.call(OWNER, 0, sel(tok.FN_TRANSFER) + be(0x55, 4) + be(250_000, 4))[0]
    assert c.storage.get(tok.balance_slot(OWNER)) == 750_000
    assert c.storage.get(tok.balance_slot(0x55)) == 250_000
    # recipient can move it onward
    assert c.call(0x55, 0, sel(tok.FN_TRANSFER) + be(0x66, 4) + be(100_000, 4))[0]
    assert c.storage.get(tok.balance_slot(0x55)) == 150_000
    assert c.storage.get(tok.balance_slot(0x66)) == 100_000


def test_token_insufficient_balance_reverts():
    c = Chain(tok.build(OWNER, SUPPLY))
    assert c.call(OWNER, 0, sel(tok.FN_INIT))[0]
    assert not c.call(0x55, 0, sel(tok.FN_TRANSFER) + be(0x66, 4) + be(1, 4))[0]   # 0x55 has nothing
    assert not c.call(OWNER, 0, sel(tok.FN_TRANSFER) + be(0x55, 4) + be(SUPPLY + 1, 4))[0]
