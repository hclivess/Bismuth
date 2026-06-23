"""poker_ref.py — pure-Python REFERENCE for the multiplayer Hold'em betting + side-pot + showdown engine
(doc/28 v2 §4). This is the SPEC and the test ORACLE for `contracts/poker_table.py`: the RV32I contract
must reproduce these exact chip movements, side-pot layers, eligibility, and award/split — bit for bit —
so the funds-critical math ([SEC-C2]/[SEC-H3]) is validated here in clear Python before the assembler.

Scope: the chip/pot logic only. Hole-card secrecy + the shuffle are off-chain (Stage 2); the 5-card hand
RANK is supplied to `settle()` (the contract reuses poker.py's already-tested `_rank5`; ranks are just
`category<<20 | tiebreak`, higher = better). Amounts are integer units (1 BIS = 1e8).

Betting model: a street closes only when EVERY still-active seat has both (a) acted voluntarily since the
last aggression and (b) matched `to_call`. A bet/raise re-opens the round (only the aggressor counts as
acted). The blinds are posted but do NOT count as acted, so the big blind always gets its option.
"""

EMPTY, ACTIVE, FOLDED, ALLIN, BUSTED = 0, 1, 2, 3, 4
RIVER = 3


class Seat:
    __slots__ = ("addr", "stack", "hand_bet", "street_bet", "state")

    def __init__(self):
        self.addr = None
        self.stack = 0
        self.hand_bet = 0      # total into the pot THIS hand (drives side-pot layering)
        self.street_bet = 0    # into THIS street (matched against to_call)
        self.state = EMPTY


class Table:
    """N-seat no-limit Hold'em hand engine. Mirrors poker_table.py's on-chain state machine."""

    def __init__(self, nseats, sb, bb):
        assert 2 <= nseats <= 9
        self.n = nseats
        self.sb, self.bb = int(sb), int(bb)
        self.seats = [Seat() for _ in range(nseats)]
        self.button = -1
        self.street = 0
        self.to_act = -1
        self.to_call = 0
        self.min_raise = self.bb
        self.acted = set()
        self.escrow = 0        # total chips committed to pots this hand (closing-invariant bound)
        self.pots = []         # [{"amount":int, "eligible":frozenset(seat)}]
        self.in_hand = False
        self.winner_by_fold = None

    # ---- seating ----
    def sit(self, seat, addr, buyin):
        s = self.seats[seat]
        assert s.state == EMPTY and not self.in_hand
        s.addr, s.stack, s.state = addr, int(buyin), ACTIVE

    def _occupied(self):
        return [i for i, s in enumerate(self.seats) if s.state not in (EMPTY, BUSTED)]

    def _next_occupied(self, frm):
        i = frm
        for _ in range(self.n):
            i = (i + 1) % self.n
            if self.seats[i].state not in (EMPTY, BUSTED):
                return i
        return -1

    # ---- start a hand ----
    def deal(self):
        occ = self._occupied()
        assert len(occ) >= 2
        for i in occ:
            s = self.seats[i]
            s.hand_bet = s.street_bet = 0
            s.state = ACTIVE
        self.button = self._next_occupied(self.button)        # rotate from STORED state [SEC-H2]
        self.escrow = 0
        self.pots = []
        self.street = 0
        self.to_call = 0
        self.min_raise = self.bb
        self.acted = set()
        self.winner_by_fold = None
        heads_up = len(occ) == 2
        sb_seat = self.button if heads_up else self._next_occupied(self.button)
        bb_seat = self._next_occupied(sb_seat)
        self._commit(sb_seat, self.sb)
        self._commit(bb_seat, self.bb)
        self.to_call = self.bb
        self.to_act = self._next_occupied(bb_seat)            # UTG (heads-up: SB/button acts first preflop)
        self.in_hand = True

    def _commit(self, seat, amount):
        """Move `amount` chips stack→street_bet/hand_bet/escrow (capped at stack). Returns chips moved."""
        s = self.seats[seat]
        amt = min(int(amount), s.stack)
        s.stack -= amt
        s.street_bet += amt
        s.hand_bet += amt
        self.escrow += amt
        if s.stack == 0 and s.state == ACTIVE:
            s.state = ALLIN
        return amt

    # ---- actions ----
    def act(self, seat, kind, amount=0):
        assert self.in_hand and seat == self.to_act, "not this seat's turn"
        s = self.seats[seat]
        assert s.state == ACTIVE
        if kind == "fold":
            s.state = FOLDED
            self.acted.add(seat)
        elif kind == "check":
            assert s.street_bet == self.to_call, "cannot check facing a bet"
            self.acted.add(seat)
        elif kind == "call":
            self._commit(seat, self.to_call - s.street_bet)
            self.acted.add(seat)
        elif kind in ("bet", "raise"):
            target = int(amount)                              # total street_bet AFTER the action
            assert target > self.to_call, "raise must exceed the current bet"
            put = target - s.street_bet
            assert put > 0
            allin = put >= s.stack
            self._commit(seat, put)
            reached = s.street_bet                            # may be < target if all-in for less
            if reached >= self.to_call + self.min_raise:      # full legal raise → reopens the round
                self.min_raise = reached - self.to_call
                self.to_call = reached
                self.acted = {seat}                           # everyone else must act again
            else:
                assert allin, "under-min raise only allowed all-in"
                if reached > self.to_call:
                    self.to_call = reached                    # all-in for less: raises the bar, does NOT reopen
                self.acted.add(seat)
        else:
            raise AssertionError("bad action %r" % kind)
        self._advance(seat)

    def _advance(self, frm):
        nonfolded = [i for i in self._occupied() if self.seats[i].state in (ACTIVE, ALLIN)]
        if len(nonfolded) == 1:                              # everyone else folded → uncontested
            self._rebuild_pots()
            self.winner_by_fold = nonfolded[0]
            self.in_hand = False
            return
        actives = [i for i in self._occupied() if self.seats[i].state == ACTIVE]
        if all((i in self.acted) and self.seats[i].street_bet == self.to_call for i in actives):
            self._close_street()
            return
        i = frm                                              # next ACTIVE seat that still owes an action
        for _ in range(self.n):
            i = (i + 1) % self.n
            s = self.seats[i]
            if s.state == ACTIVE and (i not in self.acted or s.street_bet < self.to_call):
                self.to_act = i
                return
        self._close_street()                                 # safety net (no one left to act)

    def _close_street(self):
        self._rebuild_pots()
        for s in self.seats:
            s.street_bet = 0
        actives = [i for i in self._occupied() if self.seats[i].state == ACTIVE]
        if self.street == RIVER or len(actives) <= 1:        # all bets in → showdown / runout
            self.in_hand = False
            return
        self.street += 1
        self.to_call = 0
        self.min_raise = self.bb
        self.acted = set()
        nxt = self._next_occupied(self.button)               # first active seat left of button postflop
        for _ in range(self.n):
            if self.seats[nxt].state == ACTIVE:
                break
            nxt = self._next_occupied(nxt)
        self.to_act = nxt

    # ---- side pots [SEC-C2 closing invariant, SEC-H3 eligibility] ----
    def _rebuild_pots(self):
        """Rebuild layered side pots from per-seat hand_bet. Folded players' chips STAY in the pots they
        contributed to but are NOT eligible to win them. Asserts Σ pots == Σ hand_bet == escrow."""
        contrib = {i: self.seats[i].hand_bet for i in range(self.n) if self.seats[i].hand_bet > 0}
        levels = sorted(set(contrib.values()))
        pots, prev = [], 0
        for L in levels:
            contributors = [i for i in contrib if contrib[i] >= L]
            amount = (L - prev) * len(contributors)
            if amount > 0:
                eligible = frozenset(i for i in contributors if self.seats[i].state != FOLDED)
                pots.append({"amount": amount, "eligible": eligible})
            prev = L
        merged = []                                          # merge adjacent layers with identical eligibility
        for p in pots:
            if merged and merged[-1]["eligible"] == p["eligible"]:
                merged[-1]["amount"] += p["amount"]
            else:
                merged.append(dict(p))
        self.pots = merged
        tot_pot = sum(p["amount"] for p in self.pots)
        tot_bet = sum(self.seats[i].hand_bet for i in range(self.n))
        assert tot_pot == tot_bet == self.escrow, \
            "closing invariant violated: pots=%d bets=%d escrow=%d" % (tot_pot, tot_bet, self.escrow)

    # ---- showdown / settlement [SEC-C2 checked payout, SEC-H3 odd chip] ----
    def settle(self, ranks):
        """Award each pot to its best-ranked ELIGIBLE seat; split on tie with the odd chip to the first
        winner CLOCKWISE FROM THE BUTTON within THAT pot's winner set. `ranks` = {seat: rank} for every
        seat that revealed (higher = better). Returns {addr: payout}; asserts Σ payouts == Σ pots ==
        escrow (the on-chain guarantee asserted before HALT)."""
        if not self.pots:
            self._rebuild_pots()
        payouts = {}
        for p in self.pots:
            if self.winner_by_fold is not None:               # uncontested: everyone else folded
                winners = [self.winner_by_fold] if self.winner_by_fold in p["eligible"] else sorted(p["eligible"])
            else:
                cands = [i for i in p["eligible"] if i in ranks]
                assert cands, "a contested pot must have ≥1 revealed eligible seat"
                best = max(ranks[i] for i in cands)
                winners = [i for i in cands if ranks[i] == best]
            share = p["amount"] // len(winners)
            odd = p["amount"] - share * len(winners)
            order = sorted(winners, key=lambda i: ((i - self.button - 1) % self.n))   # clockwise from button
            for k, w in enumerate(order):
                pay = share + (odd if k == 0 else 0)
                self.seats[w].stack += pay                    # winnings returned to the stack
                payouts[self.seats[w].addr] = payouts.get(self.seats[w].addr, 0) + pay
        assert sum(payouts.values()) == sum(p["amount"] for p in self.pots) == self.escrow, \
            "payout invariant violated"
        return payouts
