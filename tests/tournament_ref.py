"""tournament_ref.py — pure-Python REFERENCE / ORACLE for the Bismuth poker TOURNAMENT lifecycle
(doc/28 §6, doc/32, Stage 5). This is the SPEC and the funds-math oracle for `contracts/tournament.py`:
the RV32I contract must reproduce these exact escrow, prize-pool, blind-schedule, seating, elimination and
finisher-payout decisions — bit for bit — so the conservation invariant

    Σ FN_REGISTER buy-ins  ==  S_PRIZE_POOL  ==  Σ payouts to finishers

is validated here in clear Python before the assembler.

It deliberately REUSES the proven §4 hand engine (tests/poker_ref.Table) per logical table — exactly what
the contract does (it embeds the §4 betting/side-pot/showdown engine, namespaced per `tid`). The tournament
layer adds ONLY:
  * registration escrow -> a single prize pool (one BIS amount per entrant: the buy-in);
  * starting-chip credit (TOURNAMENT chips, NOT BIS — chips never leave custody, only the pool does);
  * a block-height blind schedule (the ONLY thing height gates besides deadlines [SEC-H2]);
  * deterministic seating from entry order (SNG: one table; MTT: round-robin across tables);
  * eliminations folded out of STORED chip state (never height-driven);
  * a finisher prize ladder paid with integer math (mulu64/divu64 in the contract), remainder deterministic.

CLOCK: block height gates DEADLINES + BLIND LEVELS ONLY. Button, seating, eliminations, odd-chip and every
payout are computed from STORED state, never from height [SEC-H2]. Transitions are idempotent / reorg-safe
[SEC-H4] (terminal flags set-once; registration idempotent per full identity).

UNITS: buy-ins / the prize pool are BIS units (1 BIS = 1e8). Tournament CHIPS are a separate accounting
unit credited at registration (starting_chips) and conserved per table by the §4 closing invariant.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from poker_ref import Table, ACTIVE, FOLDED, ALLIN, EMPTY  # the proven §4 hand engine, reused per tid

# ---- formats ----
FMT_SNG = 0   # Sit-and-Go: one table, registration flips to RUNNING when the Nth seat registers
FMT_MTT = 1   # Multi-Table: FN_START at start_height seats round-robin; late reg until late_reg_height

# ---- tournament phases ----
PH_REG = 0       # accepting registrations
PH_RUNNING = 1   # play in progress
PH_DONE = 2      # finished, prize pool fully paid


def payout_split(pool, schedule, nfin=None):
    """Split `pool` (BIS units) across finishers by `schedule` (a list of integer permille shares that sum
    to 1000, best finish first). Uses integer math identical to the contract's mulu64/divu64:
        share_k = pool * permille_k // 1000
    and the remainder (pool - Σ shares) is assigned DETERMINISTICALLY to the 1st-place finisher, so no unit
    is ever minted or burned.

    `nfin` = the number of ACTUAL finishers. When fewer entrants exist than scheduled paid places (a legal
    MTT can START with nreg < len(schedule)), the shares of the unfilled tail places are FOLDED into place 0
    (the winner always exists), so exactly `nfin` places are paid and Σ paid == pool — never burning the
    tail's units [review #1: FN_FINALIZE burn]. Returns a list of per-place BIS amounts (len == min(len
    (schedule), nfin); len(schedule) if nfin is None).
    [SEC-C3] pool*permille overflows 32 bits before the divide, hence 64-bit math in the contract."""
    assert sum(schedule) == 1000, "schedule permille must sum to 1000"
    shares = [(int(pool) * int(p)) // 1000 for p in schedule]
    rem = int(pool) - sum(shares)
    assert 0 <= rem < max(1, len(schedule)), "remainder must be < number of paid places"
    if shares:
        shares[0] += rem            # odd units to the winner, deterministically
    if nfin is not None and int(nfin) < len(shares):
        nfin = max(1, int(nfin))
        rolled = sum(shares[nfin:])          # units of places with no finisher
        shares = shares[:nfin]
        shares[0] += rolled                  # fold them up into the winner — no burn
    assert sum(shares) == int(pool), "payout split must conserve the pool exactly"
    return shares


# default ladders (permille). The contract bakes the chosen schedule at build().
SCHED_WINNER_TAKE_ALL = [1000]
SCHED_SNG_TOP3 = [500, 300, 200]            # 50 / 30 / 20
SCHED_TOP2 = [650, 350]


class Player:
    __slots__ = ("addr", "tid", "seat", "chips", "alive", "finish_place")

    def __init__(self, addr):
        self.addr = addr
        self.tid = -1
        self.seat = -1
        self.chips = 0
        self.alive = True
        self.finish_place = None   # 1 = winner, 2 = runner-up, ... assigned at elimination/finish


class Tournament:
    """Pure-Python tournament lifecycle oracle. Mirrors tournament.py's on-chain state machine. Block height
    is read ONLY for blind levels + deadlines; everything funds-critical is computed from stored state."""

    def __init__(self, fmt, buyin, starting_chips, sb_bb_levels, level_blocks,
                 schedule, target_entrants, seats_per_table=9, start_height=0, late_reg_height=0,
                 max_buyin=None):
        assert fmt in (FMT_SNG, FMT_MTT)
        self.fmt = fmt
        self.buyin = int(buyin)
        self.starting_chips = int(starting_chips)
        # blind schedule: list of (sb, bb) per level; level L active at height >= start + L*level_blocks
        self.levels = [(int(s), int(b)) for s, b in sb_bb_levels]
        assert self.levels and all(0 < s < b for s, b in self.levels)
        self.level_blocks = int(level_blocks)
        self.schedule = list(schedule)
        assert sum(self.schedule) == 1000
        self.target_entrants = int(target_entrants)
        self.seats_per_table = int(seats_per_table)
        self.start_height = int(start_height)
        self.late_reg_height = int(late_reg_height)
        self.max_buyin = int(max_buyin) if max_buyin is not None else self.buyin
        self.phase = PH_REG
        self.prize_pool = 0
        self.players = []          # entry order (deterministic seating key) [SEC-H2]
        self._by_addr = {}
        self.tables = {}           # tid -> Table (the §4 engine)
        self.running_started = False
        self.payouts = {}          # addr -> BIS paid (finisher payouts only)
        self.next_finish_from_bottom = None  # places assigned bottom-up as players bust
        self.start_block = self.start_height

    # ---------------------------------------------------------------- registration (escrow -> pool)
    def register(self, addr, buyin, height=0):
        """FN_REGISTER + buyin. Escrows the buy-in into the single prize pool (guarded), credits
        starting_chips. Idempotent per full identity [SEC-H4]: a repeat register by the same address is a
        no-op (no double escrow). Value attaches ONLY here."""
        if self.phase not in (PH_REG, PH_RUNNING):
            raise AssertionError("registration closed")
        if self.phase == PH_RUNNING:
            # late registration (MTT only, until late_reg_height)
            assert self.fmt == FMT_MTT and height <= self.late_reg_height, "late reg closed"
        assert 0 < int(buyin) <= self.max_buyin, "buyin out of range"
        assert int(buyin) == self.buyin, "must post exactly the tournament buy-in"
        if addr in self._by_addr:
            return self._by_addr[addr]            # idempotent: already registered, no re-escrow [SEC-H4]
        p = Player(addr)
        p.chips = self.starting_chips
        self.prize_pool += int(buyin)             # escrow -> pool (guarded in contract) [SEC-C3]
        self.players.append(p)
        self._by_addr[addr] = p
        # SNG: the Nth registrant flips REG -> RUNNING and seats everyone
        if self.fmt == FMT_SNG and len(self.players) == self.target_entrants:
            self._start(height)
        elif self.fmt == FMT_MTT and self.phase == PH_RUNNING:
            self._seat_player(p)                  # late entrant takes a deterministic open seat
        return p

    # ---------------------------------------------------------------- start / seating (deterministic)
    def start(self, height):
        """FN_START (MTT): at height >= start_height, seat all current registrants round-robin and begin.
        Idempotent terminal-ish flag [SEC-H4]."""
        assert self.fmt == FMT_MTT, "FN_START is MTT-only (SNG auto-starts)"
        assert height >= self.start_height, "before start_height"
        if self.running_started:
            return
        self._start(height)

    def _start(self, height):
        if self.running_started:
            return
        self.phase = PH_RUNNING
        self.running_started = True
        self.start_block = height if height else self.start_height
        # number of tables: SNG = 1; MTT = ceil(entrants / seats_per_table)
        ntables = 1 if self.fmt == FMT_SNG else max(1, (len(self.players) + self.seats_per_table - 1)
                                                    // self.seats_per_table)
        sb, bb = self.levels[0]
        for tid in range(ntables):
            self.tables[tid] = Table(self.seats_per_table if self.fmt == FMT_MTT else len(self.players), sb, bb)
        # seat round-robin from entry order [SEC-H2] (deterministic, never height/random)
        for p in self.players:
            self._seat_player(p)

    def _seat_player(self, p):
        """Deterministic seating from entry order. SNG: single table, seat == entry index. MTT: round-robin
        — entrant k goes to table (k % ntables), seat (k // ntables)."""
        idx = self.players.index(p)
        if self.fmt == FMT_SNG:
            tid, seat = 0, idx
        else:
            ntables = len(self.tables)
            tid, seat = idx % ntables, idx // ntables
        t = self.tables[tid]
        # the §4 engine credits chips as the "buy-in" (chips, not BIS) into the seat stack
        t.sit(seat, p.addr, p.chips)
        p.tid, p.seat = tid, seat

    # ---------------------------------------------------------------- blind level by HEIGHT (the one gate)
    def level_at(self, height):
        """Current blind level index for `height`. Block height gates BLIND LEVELS ONLY [SEC-H2]."""
        if height < self.start_block:
            return 0
        L = (height - self.start_block) // self.level_blocks
        return min(L, len(self.levels) - 1)

    def blinds_at(self, height):
        return self.levels[self.level_at(height)]

    # ---------------------------------------------------------------- eliminations (from STORED chips)
    def eliminate_busted(self):
        """Scan each table's STORED stacks; any seated player with 0 chips AND not in a live hand is busted.
        Assigns finish places bottom-up (last bust = highest remaining place). Deterministic; never height.
        Returns the list of newly-eliminated players (in deterministic table,seat order)."""
        newly = []
        alive_count = sum(1 for p in self.players if p.alive)
        for p in sorted((p for p in self.players if p.alive), key=lambda q: (q.tid, q.seat)):
            t = self.tables[p.tid]
            if t.seats[p.seat].stack == 0 and t.seats[p.seat].state in (EMPTY, FOLDED) and not t.in_hand:
                # only bust when not mid-hand and the seat is out of chips
                pass
        # Simpler deterministic model: a player is busted the moment their stored stack hits 0 and they are
        # not currently all-in in a live hand. Assign places in increasing (alive-rank) so the LAST to bust
        # gets the better place.
        for p in sorted((p for p in self.players if p.alive), key=lambda q: (q.tid, q.seat)):
            t = self.tables[p.tid]
            s = t.seats[p.seat]
            if s.stack == 0 and s.state != ALLIN and not t.in_hand:
                p.alive = False
                p.finish_place = alive_count          # current alive count = this player's place
                alive_count -= 1
                newly.append(p)
        # if exactly one alive remains, they win (place 1)
        alive = [p for p in self.players if p.alive]
        if len(alive) == 1 and self.phase == PH_RUNNING:
            alive[0].finish_place = 1
        return newly

    def alive_players(self):
        return [p for p in self.players if p.alive]

    # ---------------------------------------------------------------- finisher payout (the conservation pt)
    def finalize(self):
        """When the tournament is decided (1 alive), pay the finisher ladder from the prize pool with
        integer math and a deterministic remainder. Asserts Σ payouts == prize_pool [SEC-C2/C3]. Sets
        PH_DONE (set-once terminal [SEC-H4])."""
        if self.phase == PH_DONE:
            return self.payouts
        alive = self.alive_players()
        assert len(alive) <= 1, "tournament not decided yet (%d alive)" % len(alive)
        if alive:
            alive[0].finish_place = 1
        # rank everyone by finish_place (1 best). Players never given a place (shouldn't happen) sort last.
        ranked = sorted(self.players, key=lambda p: (p.finish_place if p.finish_place else 10 ** 9))
        # pay min(len(schedule), #finishers) places — folding any unfilled tail-place shares into the winner,
        # so a tournament that started with fewer entrants than paid places never burns the tail [review #1].
        shares = payout_split(self.prize_pool, self.schedule, nfin=len(self.players))
        self.payouts = {}
        for place, amt in enumerate(shares):       # place 0 -> finisher rank 1, etc.
            if amt <= 0:
                continue
            winner = ranked[place]
            self.payouts[winner.addr] = self.payouts.get(winner.addr, 0) + amt
        assert sum(self.payouts.values()) == self.prize_pool, "payout conservation violated"
        self.phase = PH_DONE
        return self.payouts

    # ---------------------------------------------------------------- conservation check
    def assert_conserved(self):
        """The headline invariant: Σ buy-ins == prize_pool == Σ payouts (once finalized)."""
        total_buyins = self.buyin * len(self.players)
        assert total_buyins == self.prize_pool, \
            "escrow conservation: Σbuyins=%d pool=%d" % (total_buyins, self.prize_pool)
        if self.phase == PH_DONE:
            assert sum(self.payouts.values()) == self.prize_pool, \
                "payout conservation: Σpayouts=%d pool=%d" % (sum(self.payouts.values()), self.prize_pool)
        return True
