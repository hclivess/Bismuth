"""betting_ref.py — pure-Python REFERENCE for the spectator parimutuel betting side-pool (doc/34). This is the
SPEC and the test ORACLE for the FN_SPECTATE_BET / FN_SETTLE_SBETS logic added additively to poker_table.py in
Stage 6: non-seated spectators stake BIS on a seated player; when the hand resolves, the pool is split
PARIMUTUEL among the backers of the winning seat(s), conserving Σ payouts == pool exactly — and if NObody
backed a winner, every bettor is refunded their own stake (never locked, never minted).

Funds model (mirrors the RV32I contract so it can be validated bit-for-bit):
  * Bets are an APPEND-ONLY list of (addr, seat, amount) in bet order — exactly the on-chain TAG_SBET records.
  * payout(bettor) = stake * pool // winstake, where winstake = Σ stakes on the winning seat(s). The product
    stake*pool needs 64 bits (the contract uses mulu64/divu64); the quotient fits 32 bits because
    payout <= pool (stake <= winstake) and the build bounds the pool < 2^32 [SEC-C3].
  * The odd-unit remainder (pool - Σ floors) goes to the FIRST winning bettor in bet order — deterministic and
    manipulation-resistant (you can't be "first" without betting first, before the outcome is known) — so
    Σ payouts == pool exactly. The contract iterates its append-only records identically.
Amounts are integer units (1 BIS = 1e8). Kept strictly separate from the table pot (S_ESCROW): the player
closing invariant is unaffected.
"""


class SpectatorPool:
    """One hand's spectator parimutuel side-pool. Mirrors poker_table.py's TAG_SBET_* storage + FN_SETTLE_SBETS."""

    def __init__(self):
        self.bets = []          # append-only [(addr, seat, amount)] in bet order (== on-chain record order)
        self.pool = 0           # S_SBET_POOL
        self.seat_total = {}    # seat -> Σ staked (TAG_SBET_SEAT|seat)

    def bet(self, addr, seat, amount):
        """Spectator `addr` stakes `amount` on seat `seat` winning. Appends a record; bumps the seat + pool."""
        amount = int(amount)
        assert amount > 0, "stake must be > 0"
        seat = int(seat)
        self.bets.append((addr, seat, amount))
        self.pool += amount
        self.seat_total[seat] = self.seat_total.get(seat, 0) + amount
        return amount

    def settle(self, winning_seats):
        """Resolve the pool against the hand's canonical winner(s). `winning_seats` = the set of seats that won
        (>=2 on a split pot). Returns {addr: payout}. Asserts Σ payouts == pool. If no winning seat was backed
        (winstake == 0 — incl. an empty pool), refunds every bettor their own stake."""
        winning = set(int(s) for s in winning_seats)
        winstake = sum(self.seat_total.get(s, 0) for s in winning)

        if winstake == 0:
            # Nobody backed a winner (or no bets at all) -> refund each bettor exactly what they staked.
            refunds = {}
            for addr, _seat, amt in self.bets:
                refunds[addr] = refunds.get(addr, 0) + amt
            assert sum(refunds.values()) == self.pool, "refund invariant"
            return refunds

        payouts = {}
        distributed = 0
        first_winner = None
        for addr, seat, amt in self.bets:
            if seat in winning:
                share = (amt * self.pool) // winstake          # mulu64 then divu64 on-chain
                payouts[addr] = payouts.get(addr, 0) + share
                distributed += share
                if first_winner is None:
                    first_winner = addr
        remainder = self.pool - distributed                    # odd units (always < #winning-bettors)
        if remainder and first_winner is not None:
            payouts[first_winner] += remainder
        assert sum(payouts.values()) == self.pool, "parimutuel conservation: Σ payouts == pool"
        return payouts

    def implied_odds(self, seat):
        """Live decimal payout multiple for a 1-unit bet on `seat` if it wins now (pool / seat stake)."""
        st = self.seat_total.get(int(seat), 0)
        return (self.pool / st) if st else 0.0
