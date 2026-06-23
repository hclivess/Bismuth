"""doc/28 v2 Stage 1 — multiplayer betting + side-pot reference vectors (tests/poker_ref.py).

These pin the funds-critical chip math the RV32I `contracts/poker_table.py` must reproduce bit-for-bit:
main + side pots from all-ins ([SEC-C2]/[SEC-H3]), all-in-for-less not reopening the round, multi-way
split with the odd chip clockwise from the button, folded-but-contributed eligibility, uncontested wins,
and the Σpots==Σbets==escrow closing invariant (asserted inside the engine on every rebuild/settle, so a
violation throws here). Run: python3 -m pytest tests/test_poker_mp.py -v
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from poker_ref import Table, ACTIVE, FOLDED, ALLIN


def _run_to_showdown_checks(t):
    """Check/check every remaining street until the hand is over (no more betting)."""
    guard = 0
    while t.in_hand:
        t.act(t.to_act, "check")
        guard += 1
        assert guard < 100


def test_headsup_call_check_to_showdown():
    t = Table(2, 1, 2)
    t.sit(0, "A0", 100); t.sit(1, "A1", 100)
    t.deal()
    assert t.button == 0 and t.to_act == 0          # heads-up: button=SB acts first preflop
    t.act(0, "call")                                # SB completes
    t.act(1, "check")                               # BB option checks
    _run_to_showdown_checks(t)
    pay = t.settle({0: 5000, 1: 10})                # seat0 wins
    assert pay == {"A0": 4}
    assert t.seats[0].stack == 102 and t.seats[1].stack == 98   # zero-sum: +2 / -2


def test_three_way_main_and_side_pot():
    t = Table(3, 1, 2)
    t.sit(0, "A0", 100); t.sit(1, "A1", 100); t.sit(2, "A2", 10)   # seat2 short
    t.deal()                                        # button0, SB=1, BB=2, UTG=0
    t.act(0, "raise", 10)
    t.act(1, "call")
    t.act(2, "call")                                # seat2 all-in for 10 total
    assert t.seats[2].state == ALLIN
    # flop: seat1 bets a side pot, seat0 calls (seat2 cannot act)
    t.act(t.to_act, "bet", 20) if t.seats[t.to_act].state == ACTIVE else None
    t.act(t.to_act, "call")
    _run_to_showdown_checks(t)
    assert len(t.pots) == 2
    assert t.pots[0]["amount"] == 30 and t.pots[0]["eligible"] == frozenset({0, 1, 2})  # main: all 3
    assert t.pots[1]["amount"] == 40 and t.pots[1]["eligible"] == frozenset({0, 1})     # side: 0,1 only
    pay = t.settle({0: 500, 1: 10, 2: 9000})        # seat2 best, seat0 second, seat1 worst
    assert pay == {"A2": 30, "A0": 40}              # seat2 wins main only; seat0 wins side
    assert sum(pay.values()) == 70


def test_all_in_for_less_does_not_reopen():
    t = Table(3, 1, 2)
    t.sit(0, "A0", 100); t.sit(1, "A1", 14); t.sit(2, "A2", 100)
    t.deal()                                        # button0, SB=1, BB=2, UTG=0
    t.act(0, "raise", 10)                           # to_call=10, min_raise=8
    assert t.to_call == 10 and t.min_raise == 8 and t.acted == {0}
    t.act(1, "raise", 14)                           # seat1 all-in for 14 < 10+8 → all-in-for-less
    assert t.seats[1].state == ALLIN
    assert t.to_call == 14                          # the bar rises…
    assert t.min_raise == 8                         # …but min_raise is UNCHANGED (no reopen)
    assert 0 in t.acted                             # the prior aggressor still counts as acted (not reset)
    # seat2 may only call/fold (can't be re-raised by seat0 off the short all-in); seat0 then just calls
    t.act(2, "call"); t.act(0, "call")
    assert not t.in_hand or t.street >= 1           # preflop closed without reopening to seat0


def test_split_pot_odd_chip_clockwise_from_button():
    t = Table(3, 1, 2)
    for i in range(3):
        t.sit(i, "A%d" % i, 100)
    t.button = 0
    t.escrow = 31
    t.pots = [{"amount": 31, "eligible": frozenset({1, 2})}]   # seats 1 & 2 tie
    pay = t.settle({1: 777, 2: 777})
    # share = 10 each (31//2=15? no: 31//2=15, odd=1). order clockwise from button0 = [1,2]; odd→seat1.
    assert pay == {"A1": 16, "A2": 15}
    assert sum(pay.values()) == 31


def test_folded_player_chips_stay_in_pot_but_not_eligible():
    t = Table(3, 1, 2)
    for i in range(3):
        t.sit(i, "A%d" % i, 100)
    t.deal()                                        # button0, SB=seat1(1), BB=seat2(2), UTG=0
    t.act(0, "call")                                # seat0 calls 2
    t.act(1, "raise", 6)                            # seat1 raises
    t.act(2, "fold")                                # seat2 (BB) folds — its 2 blind chips stay in
    t.act(0, "call")                                # seat0 calls the raise
    _run_to_showdown_checks(t)
    assert t.seats[2].state == FOLDED
    # seat2's 2 forfeited chips are in the pot; seat2 is NOT eligible
    assert sum(p["amount"] for p in t.pots) == 14
    for p in t.pots:
        assert 2 not in p["eligible"]
    pay = t.settle({0: 9000, 1: 10})                # seat0 wins
    assert pay == {"A0": 14}


def test_uncontested_everyone_folds():
    t = Table(3, 1, 2)
    for i in range(3):
        t.sit(i, "A%d" % i, 100)
    t.deal()                                        # button0, SB=1, BB=2, UTG=0
    t.act(0, "fold")
    t.act(1, "fold")                                # seat2 (BB) wins uncontested
    assert not t.in_hand and t.winner_by_fold == 2
    pay = t.settle({})                              # no reveals needed
    assert pay == {"A2": 3}                         # SB(1) + BB(2) = 3, BB nets +1
    assert sum(pay.values()) == 3


def test_closing_invariant_holds_across_a_full_three_way_hand():
    # exercises the in-engine asserts (Σpots==Σbets==escrow) on every street + at settle
    t = Table(3, 1, 2)
    t.sit(0, "A0", 50); t.sit(1, "A1", 50); t.sit(2, "A2", 50)
    t.deal()
    t.act(0, "raise", 6); t.act(1, "call"); t.act(2, "call")
    while t.in_hand:
        t.act(t.to_act, "check")
    total_in = sum(50 - t.seats[i].stack for i in range(3))   # chips that left stacks
    assert t.escrow == total_in == sum(p["amount"] for p in t.pots)
    pay = t.settle({0: 9000, 1: 10, 2: 20})
    assert sum(pay.values()) == t.escrow
