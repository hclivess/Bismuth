"""tests/test_spectator_betting.py — doc/34 Stage 6 spectator PARIMUTUEL betting, driven through
bismuth_riscv.execute on the ADDITIVE side-pool inside contracts/poker_table.py. Each vector plays a real
hand (deal -> bet -> showdown -> FN_SETTLE), with spectators placing FN_SPECTATE_BET(seat)+value during
PH_BET, then runs the permissionless FN_SETTLE_SBETS and asserts the on-chain payouts EXACTLY match the
verified ORACLE tests/betting_ref.SpectatorPool for identical inputs.

Covered: single-winner parimutuel split, odd-unit-to-first-winner, no-winner refund, split-pot (two winning
seats), a seated player rejected from betting, betting-after-close rejected, Σ payouts == pool, idempotent
set-once settle, and the PLAYER pot / custody completely UNAFFECTED by the side-pool (strict separation).

Run: python3 -m pytest tests/test_spectator_betting.py -v
"""
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "contracts"), os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import bismuth_riscv as rv
import poker_table as pt
from betting_ref import SpectatorPool


def be4(x):
    return (x & 0xFFFFFFFF).to_bytes(4, "big")


def addr28(low32):
    return bytes(24) + (low32 & 0xFFFFFFFF).to_bytes(4, "big")


def card(rank, suit):
    return suit * 13 + rank


def commit_of(cards, nonce):
    return hashlib.sha256(bytes(cards) + nonce).digest()


# five-card holdings (same engineering as test_poker_table_vm) realizing a target rank order via _rank5
HOLD_BEST = [card(12, 0), card(12, 1), card(12, 2), card(12, 3), card(11, 0)]   # quad aces (cat 7)
HOLD_MID = [card(10, 0), card(10, 1), card(10, 2), card(9, 0), card(8, 0)]      # trip queens (cat 3)
HOLD_WORST = [card(2, 1), card(4, 2), card(6, 3), card(8, 1), card(10, 3)]      # high card (cat 0)
HOLD_TIE_A = [card(12, 0), card(11, 0), card(10, 0), card(9, 0), card(8, 0)]    # straight flush hearts
HOLD_TIE_B = [card(12, 1), card(11, 1), card(10, 1), card(9, 1), card(8, 1)]    # straight flush spades (== rank)


class TableDriver:
    """Persistent-storage harness around poker_table bytecode (custody-tracking like test_poker_table_vm)."""

    def __init__(self, nseats, sb, bb, max_buyin=10000):
        self.code = pt.build(nseats, sb, bb, max_buyin=max_buyin)
        self.st = {}
        self.custody = 0
        self.height = 100
        self._seatlow = {}

    def call(self, sel, caller, tail=b"", value=0):
        return rv.execute(self.code, calldata=be4(sel) + tail, caller=caller, callvalue=value,
                          storage=dict(self.st), block_height=self.height,
                          self_balance=self.custody + value)

    def ok(self, sel, caller, tail=b"", value=0):
        r = self.call(sel, caller, tail, value)
        assert r.success, "expected success, got revert (sel %d)" % sel
        self.st = r.storage
        self.custody += value
        for _to, amt in r.transfers:
            self.custody -= amt
        self._last = r
        return r

    def reverts(self, sel, caller, tail=b"", value=0):
        assert not self.call(sel, caller, tail, value).success, "expected revert (sel %d)" % sel

    def slot(self, k):
        return self.st.get(k, 0)

    # ---- convenience ----
    def set_seats(self, *lows):
        self._seatlow = {i: lo for i, lo in enumerate(lows)}

    def sit(self, seat, low32, buyin):
        self.ok(pt.FN_SIT, low32, be4(seat) + addr28(low32), value=buyin)

    def deal(self):
        self.ok(pt.FN_DEAL, 0xDEAD)

    def commit(self, seat, low32, cards, nonce):
        self.ok(pt.FN_COMMIT, low32, commit_of(cards, nonce))

    def reveal(self, seat, low32, cards, nonce):
        self.ok(pt.FN_REVEAL, low32, bytes(cards) + nonce)

    def spectate(self, seat, low32, value):
        self.ok(pt.FN_SPECTATE_BET, low32, be4(seat) + addr28(low32), value=value)

    def phase(self):
        return self.slot(pt.S_PHASE)

    def toact(self):
        return self.slot(pt.S_TOACT)

    def escrow(self):
        return self.slot(pt.S_ESCROW)

    def check_to_showdown(self):
        guard = 0
        while self.phase() == pt.PH_BET:
            self.ok(pt.FN_CHECK, self._seatlow[self.toact()])
            guard += 1
            assert guard < 100

    def payout_transfers(self):
        out = {}
        for to, amt in self._last.transfers:
            lo = to & 0xFFFFFFFF
            out[lo] = out.get(lo, 0) + amt
        return out


def _oracle(bets, winning_seats):
    """Run tests/betting_ref.SpectatorPool over `bets` ([(low32, seat, amount)]) and return {low32: payout}."""
    p = SpectatorPool()
    for lo, seat, amt in bets:
        p.bet(lo, seat, amt)
    return p.settle(set(winning_seats)), p.pool


# ---- a reusable two-seat hand where seat 0 beats seat 1 to showdown -------------------------------------
def _play_headsup(d, winner_best=True):
    """Sit seats 0,1 (100 each), deal, drive to showdown with seat0 = HOLD_BEST (winner) and seat1 =
    HOLD_WORST. Caller places spectator bets BEFORE calling _showdown(). Returns nothing; leaves PH_SHOWDOWN."""
    d.set_seats(0xA0, 0xA1)
    d.sit(0, 0xA0, 100)
    d.sit(1, 0xA1, 100)
    d.deal()


def _showdown_headsup(d):
    d.ok(pt.FN_CALL, 0xA0)
    d.ok(pt.FN_CHECK, 0xA1)
    d.check_to_showdown()
    n0, n1 = bytes(range(32)), bytes(reversed(range(32)))
    d.commit(0, 0xA0, HOLD_BEST, n0)
    d.commit(1, 0xA1, HOLD_WORST, n1)
    d.reveal(0, 0xA0, HOLD_BEST, n0)
    d.reveal(1, 0xA1, HOLD_WORST, n1)
    d.ok(pt.FN_SETTLE, 0xDEAD)


# ============================================================ 1: single-winner parimutuel split
def test_single_winner_parimutuel_split():
    d = TableDriver(2, 1, 2)
    _play_headsup(d)
    # X:30->seat0, Y:10->seat0, Z:20->seat1 (loser) — exactly the betting_ref single-winner vector
    bets = [(0xB0, 0, 30), (0xB1, 0, 10), (0xB2, 1, 20)]
    for lo, seat, amt in bets:
        d.spectate(seat, lo, amt)
    assert d.slot(pt.S_SBET_POOL) == 60
    assert d.slot(pt.sbet_seat_key(0)) == 40 and d.slot(pt.sbet_seat_key(1)) == 20
    _showdown_headsup(d)                                # seat0 wins
    d.ok(pt.FN_SETTLE_SBETS, 0xDEAD)
    pay = d.payout_transfers()
    oracle, pool = _oracle(bets, {0})
    assert pay == oracle == {0xB0: 45, 0xB1: 15}, (pay, oracle)
    assert sum(pay.values()) == pool == 60             # Σ payouts == pool


# ============================================================ 2: odd-unit goes to the FIRST winning bettor
def test_odd_unit_to_first_winning_bettor():
    d = TableDriver(2, 1, 2)
    _play_headsup(d)
    # A,B,C each 1 on seat0; D 1 on seat1 -> pool 4, winstake 3, floors 1 each (=3), remainder 1 -> first (A)
    bets = [(0xC0, 0, 1), (0xC1, 0, 1), (0xC2, 0, 1), (0xC3, 1, 1)]
    for lo, seat, amt in bets:
        d.spectate(seat, lo, amt)
    _showdown_headsup(d)
    d.ok(pt.FN_SETTLE_SBETS, 0xDEAD)
    pay = d.payout_transfers()
    oracle, pool = _oracle(bets, {0})
    assert pay == oracle == {0xC0: 2, 0xC1: 1, 0xC2: 1}, (pay, oracle)
    assert sum(pay.values()) == pool == 4


# ============================================================ 3: no winner backed -> refund everyone
def test_no_winner_backed_refunds_everyone():
    d = TableDriver(2, 1, 2)
    _play_headsup(d)
    # everyone backs seat1 (the loser); seat0 wins -> winstake 0 -> full refund, nothing locked or minted
    bets = [(0xD0, 1, 10), (0xD1, 1, 5)]
    for lo, seat, amt in bets:
        d.spectate(seat, lo, amt)
    _showdown_headsup(d)                                # seat0 wins; nobody backed it
    d.ok(pt.FN_SETTLE_SBETS, 0xDEAD)
    pay = d.payout_transfers()
    oracle, pool = _oracle(bets, {0})
    assert pay == oracle == {0xD0: 10, 0xD1: 5}, (pay, oracle)
    assert sum(pay.values()) == pool == 15


# ============================================================ 4: split-pot (two winning seats)
def test_split_pot_two_winning_seats():
    # seats 1 & 2 tie for best, seat 0 worst -> a single main pot split -> BOTH seats 1,2 are winning seats.
    d = TableDriver(3, 1, 2)
    d.set_seats(0xA0, 0xA1, 0xA2)
    d.sit(0, 0xA0, 100)
    d.sit(1, 0xA1, 100)
    d.sit(2, 0xA2, 100)
    d.deal()                                            # button0, SB1, BB2, UTG0
    # spectators: A->seat0(loser) wait no — use the betting_ref split-pot vector: A->0,B->1,C->2; winners {1,2}
    # Map onto THIS hand: winning seats are 1 and 2 (they tie). A backs seat1, B backs seat2, C backs seat0.
    bets = [(0xE0, 0, 10), (0xE1, 1, 10), (0xE2, 2, 10)]
    for lo, seat, amt in bets:
        d.spectate(seat, lo, amt)
    # drive a 3-way all-in-call hand to a single pot, seats 1,2 tie best, seat0 worst
    d.ok(pt.FN_BET, 0xA0, be4(11))
    d.ok(pt.FN_CALL, 0xA1)
    d.ok(pt.FN_CALL, 0xA2)
    d.check_to_showdown()
    n0, n1, n2 = bytes([4]) * 32, bytes([5]) * 32, bytes([6]) * 32
    d.commit(0, 0xA0, HOLD_WORST, n0)
    d.commit(1, 0xA1, HOLD_TIE_A, n1)
    d.commit(2, 0xA2, HOLD_TIE_B, n2)
    d.reveal(0, 0xA0, HOLD_WORST, n0)
    d.reveal(1, 0xA1, HOLD_TIE_A, n1)
    d.reveal(2, 0xA2, HOLD_TIE_B, n2)
    d.ok(pt.FN_SETTLE, 0xDEAD)
    player_pay = d.payout_transfers()
    assert set(player_pay) == {0xA1, 0xA2}              # seats 1,2 split the player pot
    d.ok(pt.FN_SETTLE_SBETS, 0xDEAD)
    pay = d.payout_transfers()
    oracle, pool = _oracle(bets, {1, 2})
    # winstake = 20 (seat1 10 + seat2 10); A backed seat0 (loser) gets nothing; B,C get 15 each
    assert pay == oracle == {0xE1: 15, 0xE2: 15}, (pay, oracle)
    assert sum(pay.values()) == pool == 30


# ============================================================ 5: a SEATED player may not bet on its own hand
def test_seated_player_rejected_from_betting():
    d = TableDriver(2, 1, 2)
    _play_headsup(d)
    # seat 0's own low32 is 0xA0 -> rejected; a non-seated spectator (0xB0) is accepted
    d.reverts(pt.FN_SPECTATE_BET, 0xA0, be4(1) + addr28(0xA0), value=10)
    d.reverts(pt.FN_SPECTATE_BET, 0xA1, be4(0) + addr28(0xA1), value=10)
    d.spectate(0, 0xB0, 10)
    assert d.slot(pt.S_SBET_POOL) == 10


# ============================================================ 6: betting after the close deadline is rejected
def test_betting_after_close_rejected():
    d = TableDriver(2, 1, 2)
    _play_headsup(d)
    d.spectate(0, 0xB0, 10)                             # first bet arms S_SBET_CLOSE = height + window
    close = d.slot(pt.S_SBET_CLOSE)
    assert close == d.height + pt.SBET_CLOSE_BLOCKS
    # still inside the window: accepted
    d.spectate(0, 0xB1, 5)
    assert d.slot(pt.S_SBET_POOL) == 15
    # advance past the deadline: rejected (block height gates ONLY the betting-close, never the outcome)
    d.height = close + 1
    d.reverts(pt.FN_SPECTATE_BET, 0xB2, be4(0) + addr28(0xB2), value=5)
    assert d.slot(pt.S_SBET_POOL) == 15                # pool unchanged after the rejected bet


# ============================================================ 7: Σ payouts == pool over many shapes (oracle)
def test_conservation_and_oracle_across_shapes():
    # Each shape is (spectator bets [(low32,seat,amt)], winners-on-this-hand). We realize each on a fresh hand
    # whose showdown produces exactly `winners`, then assert the on-chain payouts == betting_ref AND Σ == pool.
    def hand_single_winner_seat0():
        d = TableDriver(2, 1, 2)
        _play_headsup(d)
        return d, lambda: _showdown_headsup(d), {0}

    shapes = [
        ([(0xF0, 0, 7), (0xF1, 0, 13), (0xF2, 1, 5)], {0}),
        ([(0xF0, 0, 100), (0xF1, 1, 1)], {0}),
        ([(0xF0, 1, 9), (0xF1, 1, 9), (0xF2, 1, 9)], {0}),      # nobody backed the winner -> refund
        ([(0xF0, 0, 3), (0xF0, 0, 5), (0xF1, 1, 2)], {0}),      # same addr two bets accumulate
    ]
    for bets, winners in shapes:
        d, showdown, w = hand_single_winner_seat0()
        assert w == winners
        for lo, seat, amt in bets:
            d.spectate(seat, lo, amt)
        showdown()
        d.ok(pt.FN_SETTLE_SBETS, 0xDEAD)
        pay = d.payout_transfers()
        oracle, pool = _oracle(bets, winners)
        assert pay == oracle, (bets, winners, pay, oracle)
        assert sum(pay.values()) == pool, (bets, winners, pay)


# ============================================================ 8: settle is idempotent (set-once) + permissionless
def test_settle_idempotent_and_permissionless():
    d = TableDriver(2, 1, 2)
    _play_headsup(d)
    bets = [(0xB0, 0, 30), (0xB1, 0, 10), (0xB2, 1, 20)]
    for lo, seat, amt in bets:
        d.spectate(seat, lo, amt)
    _showdown_headsup(d)
    # anyone may settle (here a random non-player address)
    d.ok(pt.FN_SETTLE_SBETS, 0x9999)
    first = d.payout_transfers()
    assert sum(first.values()) == 60
    # a SECOND settle is a no-op success: no further transfers, side-pool already DONE
    d.ok(pt.FN_SETTLE_SBETS, 0x123)
    assert d.payout_transfers() == {}
    assert d.slot(pt.S_SBET_DONE) == 1


# ============================================================ 9: STRICT SEPARATION — player pot/custody intact
def test_player_pot_and_custody_unaffected_by_side_pool():
    # Run the SAME player hand twice: once with NO spectator bets, once WITH them. The player payouts, the
    # player closing invariant (Σ payouts == S_ESCROW) and the post-FN_SETTLE custody must be byte-identical.
    def player_run(with_spectators):
        d = TableDriver(2, 1, 2)
        _play_headsup(d)
        if with_spectators:
            d.spectate(0, 0xB0, 30)
            d.spectate(0, 0xB1, 10)
            d.spectate(1, 0xB2, 20)
        # drive betting to showdown, capture the PLAYER escrow just before the player settle, then settle
        d.ok(pt.FN_CALL, 0xA0)
        d.ok(pt.FN_CHECK, 0xA1)
        d.check_to_showdown()
        esc = d.escrow()                                # player pot at showdown (== 4, blinds + the call)
        n0, n1 = bytes(range(32)), bytes(reversed(range(32)))
        d.commit(0, 0xA0, HOLD_BEST, n0)
        d.commit(1, 0xA1, HOLD_WORST, n1)
        d.reveal(0, 0xA0, HOLD_BEST, n0)
        d.reveal(1, 0xA1, HOLD_WORST, n1)
        d.ok(pt.FN_SETTLE, 0xDEAD)                      # the PLAYER settle
        return d, d.payout_transfers(), esc, d.custody

    d0, pay0, esc0, cust0 = player_run(False)
    d1, pay1, esc1, cust1 = player_run(True)
    assert pay0 == pay1 == {0xA0: 4}                    # player payout identical
    assert esc0 == esc1 == 4                            # player escrow identical
    # custody after the PLAYER settle: with spectators, the side-pool (60) is still escrowed (not yet settled)
    assert cust0 == 200 - 4
    assert cust1 == 200 - 4 + 60                        # exactly the still-held side-pool, nothing leaked
    # the player domain slots are untouched by the side-pool: escrow + per-seat stacks match
    assert d0.slot(pt.S_ESCROW) == d1.slot(pt.S_ESCROW)
    for s in (0, 1):
        assert d0.slot(pt.stack_key(s)) == d1.slot(pt.stack_key(s))
    # and after the spectator settle, exactly the side-pool leaves custody, player side still byte-equal
    d1.ok(pt.FN_SETTLE_SBETS, 0xDEAD)
    assert sum(d1.payout_transfers().values()) == 60
    assert d1.custody == 200 - 4                        # back to the no-spectator custody: strict separation


# ============================================================ 10: unsettled side-pool REFUNDED, never orphaned
def test_unsettled_sidepool_refunded_on_next_hand():
    # Review HIGH bug: FN_DEAL only requires the PLAYER pot settled, not the side-pool — so a new hand can start
    # with the prior side-pool unsettled. The lazy per-hand reset must REFUND those bettors before discarding
    # the pool (never silently zero it, which orphaned their stakes in custody forever). Σ refunds == old pool.
    d = TableDriver(2, 1, 2)
    _play_headsup(d)                                       # hand 1 dealt (seats 0,1)
    d.spectate(0, 0xE0, 30)                               # spectators bet during hand 1...
    d.spectate(1, 0xE1, 20)
    assert pt.sbet_pool(d.st) == 50
    _showdown_headsup(d)                                  # ...PLAYER pot settled, but the side-pool left UNSETTLED
    assert pt.sbet_pool(d.st) == 50 and d.slot(pt.S_SBET_DONE) == 0
    d.deal()                                              # deal hand 2 (allowed from PH_SETTLED) -> pool is stale
    assert d.slot(pt.S_SBET_HANDNO) != d.slot(pt.S_HANDNO)
    # a fresh spectator bet on hand 2 triggers the lazy reset, which REFUNDS hand-1's bettors before clearing
    d.ok(pt.FN_SPECTATE_BET, 0xE2, be4(0) + addr28(0xE2), value=7)
    refunds = d.payout_transfers()
    assert refunds == {0xE0: 30, 0xE1: 20}, refunds        # both stale bettors made whole — nothing orphaned
    assert sum(refunds.values()) == 50
    assert pt.sbet_pool(d.st) == 7                         # the hand-2 pool now holds only the fresh bet
    assert d.slot(pt.S_SBET_HANDNO) == d.slot(pt.S_HANDNO)


# ============================================================ 10: read helpers decode the records
def test_read_helpers_decode_records():
    d = TableDriver(2, 1, 2)
    _play_headsup(d)
    d.spectate(0, 0xB0, 30)
    d.spectate(1, 0xB7, 20)
    assert pt.sbet_count(d.st) == 2
    assert pt.sbet_pool(d.st) == 50
    r0 = pt.read_sbet(d.st, 0)
    assert r0["seat"] == 0 and r0["amount"] == 30
    assert r0["addr_hex"] == addr28(0xB0).hex()
    r1 = pt.read_sbet(d.st, 1)
    assert r1["seat"] == 1 and r1["amount"] == 20
    assert r1["addr_hex"] == addr28(0xB7).hex()
