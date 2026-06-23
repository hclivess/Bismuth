"""doc/34 Stage 6 — spectator parimutuel betting reference vectors (tests/betting_ref.py). These pin the
funds-critical settlement the RV32I FN_SETTLE_SBETS (added to poker_table.py) must reproduce bit-for-bit:
parimutuel split among backers of the winning seat(s), the odd unit to the first winning bettor, split-pot
over multiple winning seats, refund-all when no winner is backed, and Σ payouts == pool on every settle.
Run: python3 -m pytest tests/test_betting_market.py -v
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from betting_ref import SpectatorPool


def test_single_winning_seat_parimutuel_split():
    p = SpectatorPool()
    p.bet("X", 0, 30)        # backs seat 0
    p.bet("Y", 0, 10)        # backs seat 0
    p.bet("Z", 1, 20)        # backs seat 1 (loser)
    assert p.pool == 60
    pay = p.settle({0})      # seat 0 wins
    # winstake = 40; X: 30*60//40=45, Y: 10*60//40=15 -> 60; Z gets nothing
    assert pay == {"X": 45, "Y": 15}
    assert sum(pay.values()) == 60


def test_odd_unit_to_first_winning_bettor():
    p = SpectatorPool()
    p.bet("A", 0, 1)
    p.bet("B", 0, 1)
    p.bet("C", 0, 1)         # pool 3, winstake 3, each floor share = 1*3//3 = 1 -> exact, no remainder
    p.bet("D", 1, 1)         # loser, pool now 4, winstake still 3
    pay = p.settle({0})
    # shares: A=1*4//3=1, B=1, C=1 -> distributed 3, remainder 1 -> first winner A
    assert pay == {"A": 2, "B": 1, "C": 1}
    assert sum(pay.values()) == 4


def test_no_winner_backed_refunds_everyone():
    p = SpectatorPool()
    p.bet("A", 1, 10)
    p.bet("B", 2, 5)
    pay = p.settle({0})      # seat 0 won; nobody backed it
    assert pay == {"A": 10, "B": 5}     # full refund, nothing locked or minted
    assert sum(pay.values()) == p.pool


def test_empty_pool_settles_to_nothing():
    p = SpectatorPool()
    pay = p.settle({0})
    assert pay == {} and p.pool == 0


def test_split_pot_two_winning_seats():
    p = SpectatorPool()
    p.bet("A", 0, 10)
    p.bet("B", 1, 10)
    p.bet("C", 2, 10)        # loser
    pay = p.settle({0, 1})   # seats 0 and 1 split the hand -> both are winning seats
    # winstake = 20; A: 10*30//20=15, B: 15 -> 30; C nothing
    assert pay == {"A": 15, "B": 15}
    assert sum(pay.values()) == 30


def test_same_address_multiple_bets_accumulate():
    p = SpectatorPool()
    p.bet("A", 0, 10)
    p.bet("A", 0, 10)        # same bettor, two records on the winner
    p.bet("B", 1, 20)        # loser
    pay = p.settle({0})
    # winstake 20; A's two records: 10*40//20=20 each -> 40; remainder 0
    assert pay == {"A": 40}
    assert sum(pay.values()) == 40


def test_conservation_holds_across_many_shapes():
    # Σ payouts (or refunds) must always equal the pool, whatever the winner.
    shapes = [
        ([("A", 0, 7), ("B", 0, 13), ("C", 1, 5)], {0}),
        ([("A", 0, 3), ("B", 1, 3), ("C", 2, 3)], {2}),
        ([("A", 0, 100), ("B", 1, 1)], {1}),
        ([("A", 0, 9), ("B", 0, 9), ("C", 0, 9)], {1}),     # no winner backed -> refund
        ([("A", 0, 1), ("B", 1, 2), ("C", 2, 4), ("D", 0, 8)], {0, 2}),
    ]
    for bets, winners in shapes:
        p = SpectatorPool()
        for a, s, amt in bets:
            p.bet(a, s, amt)
        pay = p.settle(winners)
        assert sum(pay.values()) == p.pool, (bets, winners, pay)
        assert all(v >= 0 for v in pay.values())
