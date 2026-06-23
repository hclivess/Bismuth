"""
tournament.py — Bismuth on-chain poker TOURNAMENTS (Sit-and-Go + Multi-Table) as ONE hand-authored RV32I
contract for the in-core VM (doc/28 v2 §6, doc/32, Stage 5). Balancer-Vault model: ONE contract holds the
custody for MANY logical tables (`tid`), each running the SAME proven §4 betting/side-pot/showdown engine
(contracts/poker_table.py) — embedded here NAMESPACED PER tid so every per-table storage key is offset by
`tid * TID_STRIDE`. There are no cross-contract calls (the engine cannot make them), so one contract owns
every table's chips and the single prize pool.

THE FUNDS MODEL (conservation — asserted in-contract, validated against tests/tournament_ref.py):

    Σ FN_REGISTER buy-ins  ==  S_PRIZE_POOL  ==  Σ payouts to finishers

  * Buy-ins (BIS) attach ONLY to FN_REGISTER and are escrowed into the SINGLE S_PRIZE_POOL (guarded add).
  * Registration credits the player STARTING_CHIPS (a tournament accounting unit, NOT BIS). Chips never
    leave custody; only the prize pool does, and ONLY to finishers via a CHECKED transfer.
  * The finisher ladder (e.g. SNG 50/30/20) is computed with mulu64/divu64 [SEC-C3] (pool*permille
    overflows 32 bits before the divide), remainder to the winner deterministically (no minted/burned unit).
  * Before any HALT on the payout path, Σ payouts == S_PRIZE_POOL is asserted [SEC-C2]; transfers checked;
    never HALT on a failed transfer.

THE CLOCK [SEC-H2]: SYS_NUMBER (block height) gates DEADLINES + BLIND LEVELS ONLY. Button, seating,
eliminations, odd-chip and every payout decision come from STORED state — never height. Button is
(prev+1)%active from the stored S_BUTTON; seating is deterministic from entry order; eliminations are read
from stored chip stacks. Every permissionless transition is idempotent / reorg-safe [SEC-H4]
(read-verify-write a monotonic / set-once flag in the same call; registration idempotent per full identity).

FORMATS:
  * SNG (FMT_SNG): one logical table (tid 0). The Nth registration flips PH_REG -> PH_RUNNING and seats
    everyone (FN_DEAL then runs hands on tid 0).
  * MTT (FMT_MTT): FN_START at height >= start_height seats all current registrants round-robin across
    ceil(entrants/seats_per_table) tables; late registration is allowed until late_reg_height.

ABI (operation="vm:call", openfield="<addr>:<calldata_hex>", first 4 bytes = selector):
   0 FN_REGISTER(addr28) + BUYIN  — escrow the buy-in into the pool, credit starting chips, seat (SNG: when
                                    target reached; MTT: when running) deterministically. Idempotent.
   1 FN_START                     — (MTT) seat round-robin + begin, once height >= start_height. Idempotent.
  2..12 the §4 betting selectors, each carrying a LEADING tid (FN_DEAL(tid), FN_COMMIT(tid,..),
                                    FN_CHECK(tid), FN_CALL(tid), FN_BET(tid,target), FN_FOLD(tid),
                                    FN_REVEAL(tid,..), FN_TIMEOUT(tid), FN_SETTLE(tid)). Run the EXACT §4
                                    engine on the tid-namespaced table state. NO value attaches (revert).
  13 FN_BUST(tid)                 — permissionless: mark any 0-chip, not-all-in seat on tid busted; assign
                                    its finish place (bottom-up). Idempotent / set-once per seat.
  14 FN_FINALIZE                  — once exactly one entrant is alive across all tables, pay the finisher
                                    ladder from S_PRIZE_POOL (checked transfers, conservation asserted). PH_DONE.

Value attaches ONLY to FN_REGISTER (and the engine's internal chip moves are NOT BIS). Every other selector
runs _require_no_value and reverts on attached BIS.

This is the on-chain twin of tests/tournament_ref.py; tests/test_tournament.py drives it through
bismuth_riscv.execute and asserts the same escrow, blinds, seating, eliminations, ladder and conservation.
"""
import hashlib

from asmtools import (Asm, A0, A1, A2, A3, A4, A5, A6, S0, S1, S2, S3, T0, T1, T2, T3, T4, T5, T6, ZERO,
                      SYS_SHA256)

# ============================================================ selectors
FN_REGISTER = 0
FN_START = 1
FN_DEAL = 2
FN_COMMIT = 3
FN_CHECK = 6
FN_CALL = 7
FN_BET = 8
FN_FOLD = 9
FN_REVEAL = 10
FN_TIMEOUT = 11
FN_SETTLE = 12
FN_BUST = 13
FN_FINALIZE = 14

# ============================================================ formats / phases / seat states
FMT_SNG = 0
FMT_MTT = 1

# default finisher ladders (permille; sum to 1000) — pass one to build(schedule=...)
SCHED_WINNER_TAKE_ALL = [1000]
SCHED_SNG_TOP3 = [500, 300, 200]    # 50 / 30 / 20
SCHED_TOP2 = [650, 350]

TPH_REG = 0       # tournament accepting registrations
TPH_RUNNING = 1   # play in progress
TPH_DONE = 2      # finished, prize pool fully paid

# per-table (hand) phases — mirror poker_table
PH_OPEN = 0
PH_BET = 1
PH_SHOWDOWN = 2
PH_SETTLED = 3

ST_EMPTY = 0
ST_SEATED = 1
ST_ACTIVE = 2
ST_FOLDED = 3
ST_ALLIN = 4

# ============================================================ tournament-level global slots (NOT tid-scoped)
S_MAGIC = 0           # "PTRN" | ver
S_TPHASE = 1          # tournament phase (TPH_*)
S_PRIZE_POOL = 2      # single escrowed pool (BIS units) — Σ buy-ins == this == Σ payouts
S_NREG = 3            # number of registered entrants (entry-order high-water, drives seating) [SEC-H2]
S_NTABLES = 4         # number of logical tables once running
S_NALIVE = 5          # entrants still alive (chips > 0 or in a live hand); decremented on bust
S_NPLACES_PAID = 6    # finisher places already paid (set-once progress on FN_FINALIZE) [SEC-H4]
S_RUNNING_STARTED = 7 # set-once flag: tables seated + play begun
S_START_BLOCK = 8     # the block height at which RUNNING began (blind-schedule epoch) [SEC-H2 height->blinds]

# ============================================================ per-ENTRANT slots (by entry index e, 0..N-1)
# Entry order is the deterministic seating key [SEC-H2]. Each entrant has a full 28-byte address (7 words),
# an alive flag, a finish place (0 = still in), and its (tid, seat) once seated.
TAG_ENT_ADDR = 0x30000000   # | (e<<4) | w  (w 0..6) : 28-byte identity + payout address
TAG_ENT_ALIVE = 0x31000000  # | e : 1 while in the tournament, 0 once busted
TAG_ENT_PLACE = 0x32000000  # | e : finish place (1 = winner, 0 = still alive)
TAG_ENT_TID = 0x33000000    # | e : assigned table id
TAG_ENT_SEAT = 0x34000000   # | e : assigned seat
TAG_ENT_PAID = 0x35000000   # | e : 1 once this entrant's prize was transferred (claim-once) [SEC-H4]

# ============================================================ per-TABLE (tid-namespaced) §4 engine state
# Every per-table key is base + tid*TID_STRIDE so each logical table runs the SAME proven §4 layout in its
# own storage region. TID_STRIDE is far larger than any per-table key span (per-seat keys top out around
# 0x21000000 | (8<<4) | 10), so regions never collide for tid < 256.
TID_STRIDE = 0x01000000 << 0   # 0x01000000 per tid; per-table tags live in 0x40000000+ to clear entrant tags
TID_BASE = 0x40000000          # per-table region base (entrant tags are 0x30..0x35; pool/globals are small)

# per-table globals (offset within a tid block) — stored as TID_BASE + tid*TID_STRIDE + slot
TS_PHASE = 0
TS_DEADLINE = 1
TS_BUTTON = 2
TS_STREET = 3
TS_TOACT = 4
TS_TOCALL = 5
TS_MINRAISE = 6
TS_ESCROW = 7
TS_WINBYFOLD = 8
TS_NPOTS = 9
TS_HANDNO = 10
TS_SB = 11
TS_BB = 12

# per-table per-seat tags (offset within a tid block, OR'd with (seat<<4)|word as in poker_table)
TT_ADDR = 0x00100000      # | (seat<<4)|w (w 0..6)
TT_STACK = 0x00200000     # | seat
TT_STREETBET = 0x00300000 # | seat
TT_HANDBET = 0x00400000   # | seat
TT_STATE = 0x00500000     # | seat
TT_COMMIT = 0x00600000    # | (seat<<4)|w (w 0..7)
TT_COMMITTED = 0x00700000 # | seat
TT_ACTED = 0x00800000     # | seat
TT_RANK = 0x00900000      # | seat
TT_REVEALED = 0x00A00000  # | seat
TT_CARD = 0x00B00000      # | (seat<<4)|k (k 0..4)
TT_POTAMT = 0x00C00000    # | p
TT_POTELIG = 0x00D00000   # | p
TT_ENTIDX = 0x00E00000    # | seat : the entry index occupying this seat (links seat -> entrant)

TIMEOUT_BLOCKS = 60

# ============================================================ mem scratch (well past code + calldata)
SCRATCH = 30000
SC_RECIP = SCRATCH           # 28-byte transfer recipient
SC_AMT = SCRATCH + 32        # 8-byte BE transfer amount
SC_PRE = SCRATCH + 64        # 37-byte SHA256 preimage: c0..c4 | nonce(32)
SC_SHA = SCRATCH + 128       # 32-byte SHA256 output
SC_CARDS = SCRATCH + 192     # 5 chosen card bytes (for _rank5)
SC_CNT = SCRATCH + 216       # 13-byte rank-count array
SC_CALLER = SCRATCH + 256    # 7 words (28 bytes) caller address
SC_LEVELS = SCRATCH + 320    # pot-layering scratch (npots header word)
SC_TIDOFF = SCRATCH + 384    # 4-byte: the current call's tid*TID_STRIDE + TID_BASE (the per-table region base)
SC_ENTE = SCRATCH + 400      # 4-byte: entry index being seated (so _seat_entrant touches no caller S-reg)
SC_SEAT_TID = SCRATCH + 408  # 4-byte: computed tid for the seating
SC_SEAT_SEAT = SCRATCH + 416 # 4-byte: computed seat for the seating


# =================================================================== Python-side helpers (tests / lobby)
def party_id_of(address):
    try:
        return int(str(address), 16) & 0xFFFFFFFF
    except Exception:
        return int.from_bytes(hashlib.blake2b(str(address).encode(), digest_size=32).digest(),
                              "big") & 0xFFFFFFFF


def tid_region(tid):
    return TID_BASE + tid * TID_STRIDE


def ts_key(tid, slot):
    return tid_region(tid) + slot


def tt_key(tid, tag, sub=0):
    return tid_region(tid) + tag + sub


def stack_key(tid, seat):
    return tt_key(tid, TT_STACK, seat)


def state_key(tid, seat):
    return tt_key(tid, TT_STATE, seat)


def handbet_key(tid, seat):
    return tt_key(tid, TT_HANDBET, seat)


def potamt_key(tid, p):
    return tt_key(tid, TT_POTAMT, p)


def potelig_key(tid, p):
    return tt_key(tid, TT_POTELIG, p)


def entaddr_key(e, w):
    return TAG_ENT_ADDR | (e << 4) | w


def ent_alive_key(e):
    return TAG_ENT_ALIVE | e


def ent_place_key(e):
    return TAG_ENT_PLACE | e


def ent_tid_key(e):
    return TAG_ENT_TID | e


def ent_seat_key(e):
    return TAG_ENT_SEAT | e


def read_entrant(storage, e):
    g = lambda k: int(storage.get(k, 0))
    addr = b"".join((g(entaddr_key(e, w)) & 0xFFFFFFFF).to_bytes(4, "big") for w in range(7))
    return {"e": e, "addr_hex": addr.hex(), "alive": g(ent_alive_key(e)),
            "place": g(ent_place_key(e)), "tid": g(ent_tid_key(e)), "seat": g(ent_seat_key(e)),
            "paid": int(storage.get(TAG_ENT_PAID | e, 0))}


# =================================================================== contract build
def build(fmt, buyin, starting_chips, levels, level_blocks, schedule, target_entrants,
          nseats=9, start_height=0, late_reg_height=0, max_buyin=None):
    """Assemble the tournament contract.
      fmt           — FMT_SNG | FMT_MTT
      buyin         — exact BIS buy-in each entrant posts on FN_REGISTER (escrowed into the pool)
      starting_chips— tournament chips credited to each seat (NOT BIS; conserved per table by §4)
      levels        — [(sb, bb), ...] blind schedule; level L active at height >= start + L*level_blocks
      level_blocks  — blocks per blind level (height->blinds is the ONLY non-deadline height gate [SEC-H2])
      schedule      — finisher ladder permille (sums to 1000), best finish first (SNG_TOP3 = [500,300,200])
      target_entrants — SNG: flips REG->RUNNING when reached; MTT: capacity hint for table count
      nseats        — seats per logical table (2..9)
      start_height  — MTT FN_START gate
      late_reg_height — MTT late-registration cutoff
      max_buyin     — buy-in cap (defaults to buyin). nseats*starting_chips bounded <= 0x3FFFFFFF [SEC-C3].
    """
    fmt = int(fmt); buyin = int(buyin); starting_chips = int(starting_chips)
    nseats = int(nseats); level_blocks = int(level_blocks); target_entrants = int(target_entrants)
    start_height = int(start_height); late_reg_height = int(late_reg_height)
    if fmt not in (FMT_SNG, FMT_MTT):
        raise ValueError("fmt must be FMT_SNG or FMT_MTT")
    if not (2 <= nseats <= 9):
        raise ValueError("nseats must be 2..9")
    if buyin <= 0:
        raise ValueError("buyin must be > 0")
    if starting_chips <= 0:
        raise ValueError("starting_chips must be > 0")
    if not levels or any(not (0 < s < b) for s, b in levels):
        raise ValueError("levels need 0 < sb < bb")
    if level_blocks <= 0:
        raise ValueError("level_blocks must be > 0")
    if sum(schedule) != 1000:
        raise ValueError("schedule permille must sum to 1000")
    if len(schedule) > target_entrants and fmt == FMT_SNG:
        raise ValueError("more paid places than SNG entrants")
    if max_buyin is None:
        max_buyin = buyin
    max_buyin = int(max_buyin)
    if max_buyin < buyin:
        raise ValueError("max_buyin must be >= buyin")
    # [SEC-C3] every per-table side-pot accumulator is bounded by nseats*starting_chips
    if nseats * starting_chips > 0x3FFFFFFF:
        raise ValueError("nseats*starting_chips must be <= 0x3FFFFFFF (32-bit accumulator bound) [SEC-C3]")
    MAX_ENTRANTS = 0x10000   # entry-index space (e << 4 must stay below TAG span)
    if target_entrants > MAX_ENTRANTS:
        raise ValueError("target_entrants too large")

    # capture the blind schedule for the engine helpers (baked into bytecode at this build)
    global _LEVELS, _LEVEL_BLOCKS
    _LEVELS = [(int(s), int(b)) for s, b in levels]
    _LEVEL_BLOCKS = [level_blocks]

    a = Asm()
    N = nseats
    NL = len(levels)
    NPLACES = len(schedule)

    # ---------------- dispatch ----------------
    a.mv(S0, A0)
    _load_be32(a, T0, S0, 0)
    a.li(A2, FN_REGISTER); a.beq(T0, A2, "register")
    a.li(A2, FN_START);    a.beq(T0, A2, "start")
    a.li(A2, FN_FINALIZE); a.beq(T0, A2, "finalize")
    a.li(A2, FN_BUST);     a.beq(T0, A2, "bust")
    # the §4 betting selectors (carry a leading tid in calldata[4..8])
    a.li(A2, FN_DEAL);     a.beq(T0, A2, "deal_entry")
    a.li(A2, FN_COMMIT);   a.beq(T0, A2, "commit_entry")
    a.li(A2, FN_CHECK);    a.beq(T0, A2, "check_entry")
    a.li(A2, FN_CALL);     a.beq(T0, A2, "call_entry")
    a.li(A2, FN_BET);      a.beq(T0, A2, "bet_entry")
    a.li(A2, FN_FOLD);     a.beq(T0, A2, "fold_entry")
    a.li(A2, FN_REVEAL);   a.beq(T0, A2, "reveal_entry")
    a.li(A2, FN_TIMEOUT);  a.beq(T0, A2, "timeout_entry")
    a.li(A2, FN_SETTLE);   a.beq(T0, A2, "settle_entry")
    a.j("revert")

    # =============================================================== FN_REGISTER(addr28) + BUYIN
    # Escrow the buy-in into the single prize pool, credit starting_chips, seat deterministically.
    a.label("register")
    _sload(a, A4, S_TPHASE)
    a.li(A2, TPH_REG); a.beq(A4, A2, "reg_phase_ok$")
    # late registration (MTT only, height <= late_reg_height)
    a.li(A2, TPH_RUNNING); a.bne(A4, A2, "revert")
    if fmt != FMT_MTT:
        a.j("revert")
    else:
        a.number(A4); a.li(A2, late_reg_height); a.bltu(A2, A4, "revert")   # height > late_reg -> closed
    a.label("reg_phase_ok$")
    # buy-in = attached callvalue; must equal the tournament buy-in (exact). callvalue is masked to 32 bits.
    a.callvalue(S2)                                           # S2 = attached value (low 32)
    a.li(A2, buyin); a.bne(S2, A2, "revert")                  # exact buy-in
    # [SEC-H4] idempotent per FULL identity: if the caller (low-32) already registered, no re-escrow.
    _spill_caller(a)
    _find_entrant_of_caller(a, T4)                            # T4 = entry idx if registered, else -1 (0xFFFFFFFF)
    a.li(A2, 0xFFFFFFFF); a.bne(T4, A2, "reg_already$")       # already in -> idempotent no-op (no double pool)
    # new entrant: e = S_NREG
    _sload(a, S1, S_NREG)                                     # S1 = new entry index
    a.li(A2, target_entrants)
    if fmt == FMT_SNG:
        a.bgeu(S1, A2, "revert")                             # SNG cannot exceed target
    # store the 7-word address (identity + payout)
    for w in range(7):
        _load_be32(a, T3, S0, 4 + w * 4)
        a.li(A3, TAG_ENT_ADDR); a.slli(A4, S1, 4); a.or_(A3, A3, A4); a.li(A2, w); a.or_(A3, A3, A2)
        a.sstore(A3, T3)
    # the supplied identity word-6 must equal the authenticated caller low-32 [SEC-C1 binding]
    _load_be32(a, T4, S0, 4 + 6 * 4)
    a.li(A2, SC_CALLER + 24); a.lbu(T3, A2, 0); a.slli(T3, T3, 8)
    a.lbu(A5, A2, 1); a.or_(T3, T3, A5); a.slli(T3, T3, 8)
    a.lbu(A5, A2, 2); a.or_(T3, T3, A5); a.slli(T3, T3, 8)
    a.lbu(A5, A2, 3); a.or_(T3, T3, A5)                       # T3 = caller low-32
    a.bne(T4, T3, "revert")
    a.li(A3, TAG_ENT_ALIVE); a.or_(A3, A3, S1); a.li(A4, 1); a.sstore(A3, A4)
    a.li(A3, TAG_ENT_PLACE); a.or_(A3, A3, S1); a.sstore(A3, ZERO)
    # escrow buy-in -> S_PRIZE_POOL (guarded) [SEC-C3]
    a.li(A3, S_PRIZE_POOL); a.sload_to(A4, A3); a.add(A5, A4, S2); a.bltu(A5, A4, "revert")
    a.li(A3, S_PRIZE_POOL); a.sstore(A3, A5)
    # bump entrant count + alive count
    a.addi(A4, S1, 1); _sstore_imm(a, S_NREG, A4)
    _sload(a, A4, S_NALIVE); a.addi(A4, A4, 1); _sstore_imm(a, S_NALIVE, A4)
    # SNG: if we just reached the target, START (seat everyone, flip RUNNING). MTT: if RUNNING, seat now.
    _sload(a, A4, S_TPHASE)
    a.li(A2, TPH_RUNNING); a.beq(A4, A2, "reg_seat_now$")     # MTT late entrant -> seat into running table
    if fmt == FMT_SNG:
        a.addi(A4, S1, 1); a.li(A2, target_entrants); a.bltu(A4, A2, "reg_done$")  # not full yet
        a.j("do_start$")                                     # full -> start the SNG
    else:
        a.j("reg_done$")
    a.label("reg_seat_now$")
    _seat_entrant(a, S1, N, fmt, starting_chips)             # seat this one entrant deterministically
    a.j("reg_done$")
    a.label("reg_already$")
    a.label("reg_done$")
    a.halt()

    # =============================================================== FN_START (MTT)
    a.label("start")
    _require_no_value(a)
    if fmt != FMT_MTT:
        a.j("revert")
    else:
        _sload(a, A4, S_TPHASE); a.li(A2, TPH_REG); a.bne(A4, A2, "revert")
        a.number(A4); a.li(A2, start_height); a.bltu(A4, A2, "revert")   # before start_height
        _sload(a, A4, S_RUNNING_STARTED); a.bne(A4, ZERO, "revert")      # set-once [SEC-H4]
        a.j("do_start$")

    # ---------------- shared start path: flip RUNNING, compute table count, seat round-robin ----------------
    a.label("do_start$")
    _sload(a, A4, S_RUNNING_STARTED); a.bne(A4, ZERO, "revert")     # idempotent guard
    a.li(A4, TPH_RUNNING); _sstore_imm(a, S_TPHASE, A4)
    a.li(A4, 1); _sstore_imm(a, S_RUNNING_STARTED, A4)
    a.number(A4); _sstore_imm(a, S_START_BLOCK, A4)                 # blind-schedule epoch [SEC-H2]
    # table count: SNG -> 1 ; MTT -> ceil(nreg / N)
    _sload(a, S1, S_NREG)                                           # S1 = entrant count
    if fmt == FMT_SNG:
        a.li(A4, 1); _sstore_imm(a, S_NTABLES, A4)
    else:
        a.addi(A4, S1, N - 1)
        _div_const(a, T2, A4, N)                                    # T2 = ceil(nreg/N)
        a.li(A2, 1); a.bgeu(T2, A2, "start_nt_ok$"); a.li(T2, 1)
        a.label("start_nt_ok$")
        _sstore_imm(a, S_NTABLES, T2)
    # seat each entrant 0..nreg-1 round-robin (deterministic from entry order [SEC-H2])
    a.li(T0, 0)
    a.label("start_seat$")
    a.bgeu(T0, S1, "start_seat_done$")
    a.mv(S2, T0)                                                    # preserve loop counter across helper
    _seat_entrant(a, S2, N, fmt, starting_chips)
    a.mv(T0, S2)
    a.addi(T0, T0, 1); a.j("start_seat$")
    a.label("start_seat_done$")
    a.halt()

    # =============================================================== FN_BUST(tid)
    # Permissionless: any seat on tid with 0 chips, not all-in, and not mid-hand, is busted: its entrant's
    # alive flag clears (set-once), finish place = current alive count (bottom-up), S_NALIVE--.
    a.label("bust")
    _require_no_value(a)
    _sload(a, A4, S_TPHASE); a.li(A2, TPH_RUNNING); a.bne(A4, A2, "revert")
    _load_tid(a, S1)                                                # S1 = tid (validated < ntables)
    _tid_offset(a, S1)                                              # SC_TIDOFF = region base
    # table must not be mid-hand (phase OPEN or SETTLED)
    _tload(a, A4, TS_PHASE); a.li(A2, PH_BET); a.beq(A4, A2, "revert"); a.li(A2, PH_SHOWDOWN); a.beq(A4, A2, "revert")
    a.li(T0, 0)
    a.label("bust_scan$")
    a.li(A2, N); a.bgeu(T0, A2, "bust_done$")
    _tt_load(a, A4, TT_STATE, T0); a.beq(A4, ZERO, "bust_next$")    # empty seat (already busted/never seated)
    # bust runs ONLY when the hand is not live (phase OPEN/SETTLED, checked above), so a 0-chip seat — even
    # one left ST_ALLIN by the just-settled hand — is genuinely out of chips and busts.
    _tt_load(a, A4, TT_STACK, T0); a.bne(A4, ZERO, "bust_next$")    # has chips -> alive
    # 0-chip seat -> bust its entrant if still alive (set-once)
    _tt_load(a, S2, TT_ENTIDX, T0)                                  # S2 = entry index at this seat
    a.li(A3, TAG_ENT_ALIVE); a.or_(A3, A3, S2); a.sload_to(A4, A3); a.beq(A4, ZERO, "bust_next$")  # already busted
    a.li(A3, TAG_ENT_ALIVE); a.or_(A3, A3, S2); a.sstore(A3, ZERO)  # clear alive (set-once)
    # finish place = current alive count (BEFORE decrement); deterministic bottom-up [SEC-H2/H4]. Keep the
    # place in T1 (an S-reg-safe temp) so the later _tt_store_imm (which uses A4 for its immediate) can't
    # clobber it before we decrement S_NALIVE.
    _sload(a, T1, S_NALIVE)                                         # T1 = alive count = this seat's place
    a.li(A3, TAG_ENT_PLACE); a.or_(A3, A3, S2); a.sstore(A3, T1)
    a.addi(T1, T1, -1); _sstore_imm(a, S_NALIVE, T1)               # S_NALIVE-- (computed before any A4 use)
    # mark seat EMPTY so it leaves the §4 engine (chips already 0; no funds move)
    _tt_store_imm(a, TT_STATE, T0, ST_EMPTY)
    a.label("bust_next$")
    a.addi(T0, T0, 1); a.j("bust_scan$")
    a.label("bust_done$")
    a.halt()

    # =============================================================== FN_FINALIZE
    # When exactly one entrant is alive, pay the finisher ladder from the prize pool. The lone survivor gets
    # place 1. Conservation asserted before any transfer [SEC-C2/C3]; checked transfers; PH_DONE set-once.
    a.label("finalize")
    _require_no_value(a)
    _sload(a, A4, S_TPHASE); a.li(A2, TPH_RUNNING); a.bne(A4, A2, "revert")
    _sload(a, A4, S_NALIVE); a.li(A2, 1); a.bgeu(A4, A2, "fin_one_check$"); a.j("revert")
    a.label("fin_one_check$")
    _sload(a, A4, S_NALIVE); a.li(A2, 1); a.bne(A4, A2, "revert")  # exactly one alive -> decided
    # the lone alive entrant gets place 1
    _sload(a, S3, S_NREG)
    a.li(T0, 0)
    a.label("fin_winner$")
    a.bgeu(T0, S3, "fin_winner_done$")
    a.li(A3, TAG_ENT_ALIVE); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.beq(A4, ZERO, "fin_winner_next$")
    a.li(A3, TAG_ENT_PLACE); a.or_(A3, A3, T0); a.li(A4, 1); a.sstore(A3, A4)
    a.li(A3, TAG_ENT_ALIVE); a.or_(A3, A3, T0); a.sstore(A3, ZERO) # finished -> not alive
    a.li(A4, 0); _sstore_imm(a, S_NALIVE, A4)
    a.j("fin_winner_done$")
    a.label("fin_winner_next$")
    a.addi(T0, T0, 1); a.j("fin_winner$")
    a.label("fin_winner_done$")
    _finalize_payout(a, schedule, buyin)

    # =============================================================== §4 betting selectors (tid-namespaced)
    # Each loads + validates the leading tid, sets SC_TIDOFF (the region base added to every per-table key),
    # then runs the EXACT §4 engine on the namespaced state. They share the engine body below.
    _emit_engine(a, N, int(buyin), int(max_buyin), starting_chips)

    a.label("revert")
    a.raw(0)
    return a.assemble()


# =================================================================== low-level helpers
def _load_be32(a, rd, base_reg, off):
    a.lbu(rd, base_reg, off)
    a.slli(rd, rd, 8)
    a.lbu(A2, base_reg, off + 1); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8)
    a.lbu(A2, base_reg, off + 2); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8)
    a.lbu(A2, base_reg, off + 3); a.or_(rd, rd, A2)


def _sload(a, dst, slot):
    a.li(A3, slot); a.sload_to(dst, A3)


def _sstore_imm(a, slot, val_reg):
    a.li(A3, slot); a.sstore(A3, val_reg)


def _require_no_value(a):
    a.callvalue(A2); a.bne(A2, ZERO, "revert")


def _set_deadline(a):
    a.number(A4); a.li(A2, TIMEOUT_BLOCKS); a.add(A4, A4, A2)
    _ts_store_imm(a, TS_DEADLINE, A4)


def _spill_caller(a):
    """Spill the caller's authenticated low-32 to SC_CALLER+24 (BE bytes). SYS_CALLER exposes only low 32
    bits; identity binds to those 32 bits (shared engine limitation, see poker_table)."""
    a.caller(A4)
    a.li(A2, SC_CALLER + 24)
    a.srli(A6, A4, 24); a.sb(A6, A2, 0)
    a.srli(A6, A4, 16); a.sb(A6, A2, 1)
    a.srli(A6, A4, 8); a.sb(A6, A2, 2)
    a.sb(A4, A2, 3)


def _caller_low32(a, dst):
    a.li(A2, SC_CALLER + 24); a.lbu(dst, A2, 0); a.slli(dst, dst, 8)
    a.lbu(A5, A2, 1); a.or_(dst, dst, A5); a.slli(dst, dst, 8)
    a.lbu(A5, A2, 2); a.or_(dst, dst, A5); a.slli(dst, dst, 8)
    a.lbu(A5, A2, 3); a.or_(dst, dst, A5)


def _find_entrant_of_caller(a, dst):
    """dst = entry index whose stored addr word-6 == caller low-32, else 0xFFFFFFFF. Scans 0..S_NREG-1.
    _spill_caller must have run. Uses T0..T1,A2..A6,dst."""
    _caller_low32(a, A6)                                       # A6 = caller low-32
    loop = a._uniq("foc"); hit = a._uniq("foc_hit"); nxt = a._uniq("foc_next"); done = a._uniq("foc_done")
    a.li(dst, 0xFFFFFFFF)
    _sload(a, T1, S_NREG)
    a.li(T0, 0)
    a.label(loop)
    a.bgeu(T0, T1, done)
    a.li(A3, TAG_ENT_ADDR); a.slli(A5, T0, 4); a.or_(A3, A3, A5); a.li(A2, 6); a.or_(A3, A3, A2)
    a.sload_to(A4, A3)
    a.beq(A4, A6, hit)
    a.label(nxt)
    a.addi(T0, T0, 1); a.j(loop)
    a.label(hit)
    a.mv(dst, T0)
    a.label(done)


def _div_const(a, rq, ra, d):
    """rq = ra // d for a small positive constant d (2..9). Repeated subtraction (ra small: <= entrants).
    Uses A5,A6. ra preserved? No — copies first. Uses rq, A5, A6 and a temp."""
    loop = a._uniq("dc")
    a.li(rq, 0)
    a.mv(A6, ra)
    a.label(loop)
    a.li(A5, d); a.bltu(A6, A5, "%s_done" % loop)
    a.li(A5, d); a.sub(A6, A6, A5); a.addi(rq, rq, 1); a.j(loop)
    a.label("%s_done" % loop)


def _mod_const(a, rd, ra, d):
    """rd = ra mod d for small positive constant d. Uses A5. ra preserved via copy into rd."""
    loop = a._uniq("mc")
    a.mv(rd, ra)
    a.label(loop)
    a.li(A5, d); a.bltu(rd, A5, "%s_done" % loop)
    a.li(A5, d); a.sub(rd, rd, A5); a.j(loop)
    a.label("%s_done" % loop)


# ---------------- per-table (tid-namespaced) storage access ----------------
def _load_tid(a, dst):
    """dst = the leading tid from calldata[4..8]; revert if tid >= S_NTABLES. Uses A2..A4,dst."""
    _load_be32(a, dst, S0, 4)
    _sload(a, A4, S_NTABLES); a.bgeu(dst, A4, "revert")


def _tid_offset(a, tid_reg):
    """Compute the per-table region base = TID_BASE + tid*TID_STRIDE and store it at SC_TIDOFF. Uses
    A2,A4,A5,A6. tid_reg preserved."""
    # NB: mulu uses A6 as inner scratch, so tmp_shift/tmp_mcand must avoid A6 [author gotcha].
    a.li(A2, TID_STRIDE)
    a.mulu(A4, tid_reg, A2, tmp_shift=T5, tmp_mcand=T6)        # A4 = tid*TID_STRIDE
    a.li(A2, TID_BASE); a.add(A4, A4, A2)
    a.li(A2, SC_TIDOFF); a.sw(A4, A2, 0)


def _tidoff(a, dst):
    a.li(A2, SC_TIDOFF); a.lw(dst, A2, 0)


def _ts_key(a, dst, slot):
    """dst = region_base + slot (a per-table global key). Uses A2,dst."""
    _tidoff(a, dst); a.li(A2, slot); a.add(dst, dst, A2)


def _ts_load(a, dst, slot):
    _ts_key(a, A3, slot); a.sload_to(dst, A3)


# alias used in bust/finalize paths where SC_TIDOFF already set
def _tload(a, dst, slot):
    _ts_load(a, dst, slot)


def _ts_store_imm(a, slot, val_reg):
    _ts_key(a, A3, slot); a.sstore(A3, val_reg)


def _tt_key(a, dst, tag, sub_reg):
    """dst = region_base + tag + sub_reg (per-seat / per-pot key). Uses A2,dst."""
    _tidoff(a, dst); a.li(A2, tag); a.add(dst, dst, A2); a.add(dst, dst, sub_reg)


def _tt_key_seatword(a, dst, tag, seat_reg, word):
    """dst = region_base + tag + (seat<<4) + word."""
    _tidoff(a, dst); a.li(A2, tag); a.add(dst, dst, A2)
    a.slli(A2, seat_reg, 4); a.add(dst, dst, A2); a.li(A2, word); a.add(dst, dst, A2)


def _tt_load(a, dst, tag, sub_reg):
    _tt_key(a, A3, tag, sub_reg); a.sload_to(dst, A3)


def _tt_store(a, tag, sub_reg, val_reg):
    _tt_key(a, A3, tag, sub_reg); a.sstore(A3, val_reg)


def _tt_store_imm(a, tag, sub_reg, imm):
    _tt_key(a, A3, tag, sub_reg); a.li(A4, imm); a.sstore(A3, A4)


# ---------------- seating ----------------
def _seat_entrant(a, e_reg, N, fmt, starting_chips):
    """Seat entrant e_reg deterministically [SEC-H2]. SNG: tid 0, seat = e. MTT: tid = e % ntables,
    seat = e // ntables. Credits starting_chips into the §4 seat stack (chips, not BIS), copies the
    entrant's 7-word address into the table seat addr, records (tid,seat) on the entrant and the entry index
    on the seat. Uses many temps; e_reg must be an S-reg (preserved by caller)."""
    # Self-contained: spill e to SC_ENTE (mem) and use ONLY T-registers + mem, so callers' S-registers
    # (loop bounds / entry index) survive. tid -> SC_SEAT_TID (mem), seat -> SC_SEAT_SEAT (mem).
    a.li(A2, SC_ENTE); a.sw(e_reg, A2, 0)                     # SC_ENTE = e
    if fmt == FMT_SNG:
        a.li(T0, 0)                                           # tid = 0
        a.li(A2, SC_ENTE); a.lw(T1, A2, 0)                    # seat = e
    else:
        _sload(a, A4, S_NTABLES)
        a.li(A2, SC_ENTE); a.lw(T2, A2, 0)
        _mod_const_reg(a, T0, T2, A4)                         # T0 = e % ntables (tid)
        a.li(A2, SC_ENTE); a.lw(T2, A2, 0)
        _div_const_reg(a, T1, T2, A4)                         # T1 = e // ntables (seat)
    a.li(A2, SC_SEAT_TID); a.sw(T0, A2, 0)
    a.li(A2, SC_SEAT_SEAT); a.sw(T1, A2, 0)
    _tid_offset(a, T0)                                        # SC_TIDOFF for this tid
    # copy 7-word address from entrant -> table seat addr (re-read e/seat from mem each iteration)
    for w in range(7):
        a.li(A2, SC_ENTE); a.lw(T2, A2, 0)
        a.li(A3, TAG_ENT_ADDR); a.slli(A4, T2, 4); a.or_(A3, A3, A4); a.li(A2, w); a.or_(A3, A3, A2)
        a.sload_to(T3, A3)
        a.li(A2, SC_SEAT_SEAT); a.lw(T4, A2, 0)
        _tt_key_seatword(a, A3, TT_ADDR, T4, w); a.sstore(A3, T3)
    # stack = starting_chips ; state = SEATED ; entidx = e
    a.li(A2, SC_SEAT_SEAT); a.lw(T4, A2, 0)
    a.li(T3, starting_chips); _tt_store(a, TT_STACK, T4, T3)
    a.li(A2, SC_SEAT_SEAT); a.lw(T4, A2, 0)
    a.li(T3, ST_SEATED); _tt_store(a, TT_STATE, T4, T3)
    a.li(A2, SC_ENTE); a.lw(T3, A2, 0)
    a.li(A2, SC_SEAT_SEAT); a.lw(T4, A2, 0)
    _tt_store(a, TT_ENTIDX, T4, T3)
    # record (tid, seat) on the entrant
    a.li(A2, SC_ENTE); a.lw(T3, A2, 0)
    a.li(A2, SC_SEAT_TID); a.lw(T0, A2, 0)
    a.li(A3, TAG_ENT_TID); a.or_(A3, A3, T3); a.sstore(A3, T0)
    a.li(A2, SC_SEAT_SEAT); a.lw(T1, A2, 0)
    a.li(A3, TAG_ENT_SEAT); a.or_(A3, A3, T3); a.sstore(A3, T1)


def _mod_const_reg(a, rd, ra, rdiv):
    """rd = ra mod rdiv (rdiv a register, small). Uses A5. ra preserved via copy into rd."""
    loop = a._uniq("mcr")
    a.mv(rd, ra)
    a.label(loop)
    a.bltu(rd, rdiv, "%s_done" % loop)
    a.sub(rd, rd, rdiv); a.j(loop)
    a.label("%s_done" % loop)


def _div_const_reg(a, rd, ra, rdiv):
    """rd = ra // rdiv (rdiv a register, small). Uses A5,A6. ra preserved via copy."""
    loop = a._uniq("dcr")
    a.li(rd, 0)
    a.mv(A6, ra)
    a.label(loop)
    a.bltu(A6, rdiv, "%s_done" % loop)
    a.sub(A6, A6, rdiv); a.addi(rd, rd, 1); a.j(loop)
    a.label("%s_done" % loop)


# ---------------- finisher payout (the conservation point) ----------------
def _finalize_payout(a, schedule, buyin):
    """Pay the finisher ladder from S_PRIZE_POOL. shares baked from `schedule` (permille). Uses mulu64/divu64
    [SEC-C3]: share_k = pool*permille_k // 1000. Remainder (pool - Σshares) goes to place 1 deterministically.
    Conservation asserted (Σ payouts == pool) BEFORE any transfer [SEC-C2]; checked transfers; PH_DONE."""
    NPLACES = len(schedule)
    # Step 1: compute each place's share into mem at SC_LEVELS+4*k, summing for the remainder.
    _sload(a, S1, S_PRIZE_POOL)                                # S1 = pool
    a.li(T6, 0)                                                # T6 = running Σ shares
    for k, pm in enumerate(schedule):
        a.li(A2, pm)                                           # permille
        # (T0:T1) = pool * permille  (64-bit) ; share = product // 1000
        a.mulu64(T0, T1, S1, A2, m_hi=T2, m_lo=T3, bits=T4, carry=A5)
        a.li(A4, 1000)
        a.divu64(T5, T0, T1, A4, rem=T2, bit=T3, i=T4)        # T5 = share_k (rq must avoid A6 scratch)
        a.li(A2, SC_LEVELS + 4 * k); a.sw(T5, A2, 0)
        a.add(T6, T6, T5)
    # remainder = pool - Σ shares -> add to place 0 (the winner) deterministically
    a.sub(A6, S1, T6)                                          # A6 = remainder
    a.li(A2, SC_LEVELS + 0); a.lw(A4, A2, 0); a.add(A4, A4, A6); a.sw(A4, A2, 0)
    # [review #1 fix] An MTT can legally START with fewer entrants than scheduled paid places (FN_START seats
    # whoever registered). A place k >= nreg has NO finisher, so Step 3 would never transfer its share and the
    # units would be BURNED — while the old "Σ COMPUTED shares == pool" assert still passed. Fold every
    # unfilled place's share (k >= nreg) into place 0 (the winner, which always exists), so Σ TRANSFERRED == pool.
    _sload(a, T5, S_NREG)                                      # T5 = nreg (== number of finishers)
    for k in range(1, NPLACES):
        keep = a._uniq("fp_keep")
        a.li(A2, k); a.bltu(A2, T5, keep)                     # k < nreg -> filled place, keep its share
        a.li(A2, SC_LEVELS + 4 * k); a.lw(A4, A2, 0)          # A4 = unfilled place k's share
        a.li(A3, SC_LEVELS + 0); a.lw(A5, A3, 0); a.add(A5, A5, A4); a.sw(A5, A3, 0)  # place0 += it
        a.li(A2, SC_LEVELS + 4 * k); a.sw(ZERO, A2, 0)        # zero the unfilled place (Step 3 skips it)
        a.label(keep)
    # Step 2: conservation — Σ shares (post-remainder, post-fold) == pool. Recompute the sum and assert.
    a.li(T6, 0)
    for k in range(NPLACES):
        a.li(A2, SC_LEVELS + 4 * k); a.lw(A4, A2, 0); a.add(T6, T6, A4)
    a.bne(T6, S1, "revert")                                    # [SEC-C2] conservation before any transfer
    # also assert pool == buyin * nreg (escrow conservation)
    _sload(a, A4, S_NREG); a.li(A2, buyin)
    a.mulu(T0, A4, A2, tmp_shift=T1, tmp_mcand=T2)             # T0 = buyin*nreg
    a.bne(T0, S1, "revert")
    # set PH_DONE + places-paid (set-once terminal) BEFORE transfers [SEC-H4]
    a.li(A4, TPH_DONE); _sstore_imm(a, S_TPHASE, A4)
    a.li(A4, NPLACES); _sstore_imm(a, S_NPLACES_PAID, A4)
    # Step 3: for each place k (0..NPLACES-1), find the entrant with finish_place == k+1 and pay share_k.
    a.li(S2, 0)                                                # S2 = place index k
    a.label("fp_place$")
    a.li(A2, NPLACES); a.bgeu(S2, A2, "fp_done$")
    a.li(A2, SC_LEVELS); a.slli(A4, S2, 2); a.add(A2, A2, A4); a.lw(S3, A2, 0)   # S3 = share_k
    a.beq(S3, ZERO, "fp_next$")                                # zero share -> skip
    a.addi(T4, S2, 1)                                          # target finish place = k+1
    # scan entrants for finish_place == T4 and not yet paid
    _sload(a, T5, S_NREG)
    a.li(T0, 0)
    a.label("fp_scan$")
    a.bgeu(T0, T5, "fp_next$")                                 # no matching place -> skip (defensive)
    a.li(A3, TAG_ENT_PLACE); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.bne(A4, T4, "fp_scan_next$")
    a.li(A3, TAG_ENT_PAID); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.bne(A4, ZERO, "fp_scan_next$")
    # pay entrant T0 the share S3 (checked transfer to its 28-byte address)
    a.li(A3, TAG_ENT_PAID); a.or_(A3, A3, T0); a.li(A4, 1); a.sstore(A3, A4)     # set-once before transfer
    a.mv(S1, T0)                                               # S1 = entrant idx (S-reg, preserved by pay)
    _pay_entrant_checked(a, S1, S3)
    a.j("fp_next$")
    a.label("fp_scan_next$")
    a.addi(T0, T0, 1); a.j("fp_scan$")
    a.label("fp_next$")
    a.addi(S2, S2, 1); a.j("fp_place$")
    a.label("fp_done$")
    a.halt()


def _pay_entrant_checked(a, e_reg, amount_reg):
    """CHECKED transfer of amount_reg BIS to entrant e_reg's stored 28-byte address [SEC-C2]. e_reg/amount_reg
    must be S-regs (preserved). Uses T0..T4,A2..A6."""
    loop = a._uniq("pe"); done = a._uniq("pe_done")
    a.li(T0, 0)
    a.label(loop)
    a.li(A2, 7); a.bgeu(T0, A2, done)
    a.li(A3, TAG_ENT_ADDR); a.slli(A4, e_reg, 4); a.or_(A3, A3, A4); a.add(A3, A3, T0); a.sload_to(A4, A3)
    a.li(A2, SC_RECIP); a.slli(A6, T0, 2); a.add(A2, A2, A6)
    a.srli(A6, A4, 24); a.sb(A6, A2, 0)
    a.srli(A6, A4, 16); a.sb(A6, A2, 1)
    a.srli(A6, A4, 8); a.sb(A6, A2, 2)
    a.sb(A4, A2, 3)
    a.addi(T0, T0, 1); a.j(loop)
    a.label(done)
    a.li(A3, SC_AMT); a.store_u64_be(amount_reg, A3, 0)
    a.li(A4, SC_RECIP); a.li(A5, SC_AMT); a.transfer(A4, A5)
    a.li(A2, 1); a.bne(A0, A2, "revert")                       # CHECKED [SEC-C2]


# =================================================================== the embedded §4 engine (tid-namespaced)
def _emit_engine(a, N, buyin, max_buyin, starting_chips):
    """Emit the §4 betting/side-pot/showdown engine, every per-table storage access namespaced by the current
    call's tid (region base at SC_TIDOFF). This is the proven poker_table.py engine re-targeted to the
    tid-scoped key layout. Blind levels are looked up by HEIGHT [SEC-H2]; nothing chip/seat/button reads
    height. The selectors each load+validate tid, set SC_TIDOFF, then jump into the shared body.
    """
    # ---- selector entries: load tid, set offset, dispatch into the engine body ----
    a.label("deal_entry")
    _require_no_value(a); _load_tid(a, S1); _tid_offset(a, S1); a.j("eng_deal$")
    a.label("commit_entry")
    _require_no_value(a); _load_tid(a, S1); _tid_offset(a, S1); a.j("eng_commit$")
    a.label("check_entry")
    _require_no_value(a); _load_tid(a, S1); _tid_offset(a, S1); a.j("eng_check$")
    a.label("call_entry")
    _require_no_value(a); _load_tid(a, S1); _tid_offset(a, S1); a.j("eng_call$")
    a.label("bet_entry")
    _require_no_value(a); _load_tid(a, S1); _tid_offset(a, S1); a.j("eng_bet$")
    a.label("fold_entry")
    _require_no_value(a); _load_tid(a, S1); _tid_offset(a, S1); a.j("eng_fold$")
    a.label("reveal_entry")
    _require_no_value(a); _load_tid(a, S1); _tid_offset(a, S1); a.j("eng_reveal$")
    a.label("timeout_entry")
    _require_no_value(a); _load_tid(a, S1); _tid_offset(a, S1); a.j("eng_timeout$")
    a.label("settle_entry")
    _require_no_value(a); _load_tid(a, S1); _tid_offset(a, S1); a.j("eng_settle$")

    # =============================================================== FN_DEAL(tid)
    a.label("eng_deal$")
    _ts_load(a, A4, TS_PHASE)
    a.li(A2, PH_OPEN); a.beq(A4, A2, "eng_deal_ok$")
    a.li(A2, PH_SETTLED); a.beq(A4, A2, "eng_deal_ok$")
    a.j("revert")
    a.label("eng_deal_ok$")
    # count occupied (state != EMPTY) -> need >= 2
    a.li(T1, 0); a.li(T0, 0)
    a.label("eng_deal_cnt$")
    a.li(A2, N); a.bgeu(T0, A2, "eng_deal_cnt_done$")
    _tt_load(a, A4, TT_STATE, T0); a.beq(A4, ZERO, "eng_deal_cnt_next$")
    a.addi(T1, T1, 1)
    a.label("eng_deal_cnt_next$")
    a.addi(T0, T0, 1); a.j("eng_deal_cnt$")
    a.label("eng_deal_cnt_done$")
    a.li(A2, 2); a.bltu(T1, A2, "revert")
    # reset per-seat hand state: occupied -> ACTIVE, clear bets/acted/revealed/rank/committed
    a.li(T0, 0)
    a.label("eng_deal_reset$")
    a.li(A2, N); a.bgeu(T0, A2, "eng_deal_reset_done$")
    _tt_load(a, A4, TT_STATE, T0); a.beq(A4, ZERO, "eng_deal_reset_next$")
    _tt_store_imm(a, TT_STATE, T0, ST_ACTIVE)
    _tt_store(a, TT_HANDBET, T0, ZERO)
    _tt_store(a, TT_STREETBET, T0, ZERO)
    _tt_store(a, TT_ACTED, T0, ZERO)
    _tt_store(a, TT_REVEALED, T0, ZERO)
    _tt_store(a, TT_RANK, T0, ZERO)
    _tt_store(a, TT_COMMITTED, T0, ZERO)
    a.label("eng_deal_reset_next$")
    a.addi(T0, T0, 1); a.j("eng_deal_reset$")
    a.label("eng_deal_reset_done$")
    # button rotate from STORED state [SEC-H2]; first hand (handno==0) starts from -1
    _ts_load(a, A5, TS_HANDNO)
    a.bne(A5, ZERO, "eng_deal_btn$")
    a.li(A5, 0xFFFFFFFF); a.j("eng_deal_btn_have$")
    a.label("eng_deal_btn$")
    _ts_load(a, A5, TS_BUTTON)
    a.label("eng_deal_btn_have$")
    _eng_next_occupied(a, S2, A5, N)                          # S2 = new button
    _ts_store_imm(a, TS_BUTTON, S2)
    # blinds by HEIGHT level [SEC-H2 height->blinds]: look up (sb,bb) for the current level into S_? then post
    _eng_blinds_for_height(a, N)                              # sets TS_SB / TS_BB from the height schedule
    # heads-up? count occupied again
    a.li(T1, 0); a.li(T0, 0)
    a.label("eng_deal_oc$")
    a.li(A2, N); a.bgeu(T0, A2, "eng_deal_oc_done$")
    _tt_load(a, A4, TT_STATE, T0); a.beq(A4, ZERO, "eng_deal_oc_next$")
    a.addi(T1, T1, 1)
    a.label("eng_deal_oc_next$")
    a.addi(T0, T0, 1); a.j("eng_deal_oc$")
    a.label("eng_deal_oc_done$")
    a.li(A2, 2); a.beq(T1, A2, "eng_deal_hu$")
    _eng_next_occupied(a, S3, S2, N)                          # S3 = SB seat (after button)
    a.j("eng_deal_blinds$")
    a.label("eng_deal_hu$")
    a.mv(S3, S2)                                              # heads-up: SB == button
    a.label("eng_deal_blinds$")
    # BB seat in T6 (need both S2=button preserved; use a temp for BB)
    _eng_next_occupied(a, S1, S3, N)                          # S1 = BB seat
    # reset escrow/street/winbyfold
    a.li(A4, 0); _ts_store_imm(a, TS_ESCROW, A4)
    a.li(A4, 0); _ts_store_imm(a, TS_STREET, A4)
    a.li(A4, 0); _ts_store_imm(a, TS_WINBYFOLD, A4)
    # post SB then BB (amounts from TS_SB / TS_BB)
    _ts_load(a, A1, TS_SB); _eng_commit_chips(a, S3, "eng_csb")
    _ts_load(a, A1, TS_BB); _eng_commit_chips(a, S1, "eng_cbb")
    _ts_load(a, A4, TS_BB); _ts_store_imm(a, TS_TOCALL, A4)
    _ts_load(a, A4, TS_BB); _ts_store_imm(a, TS_MINRAISE, A4)
    _eng_next_occupied(a, S2, S1, N); _ts_store_imm(a, TS_TOACT, S2)   # UTG = next after BB
    a.li(A4, PH_BET); _ts_store_imm(a, TS_PHASE, A4)
    _ts_load(a, A4, TS_HANDNO); a.addi(A4, A4, 1); _ts_store_imm(a, TS_HANDNO, A4)
    _set_deadline(a)
    a.halt()

    # =============================================================== FN_COMMIT(tid, commit32)
    a.label("eng_commit$")
    _ts_load(a, A4, TS_PHASE); a.li(A2, PH_BET); a.bltu(A4, A2, "revert")
    a.li(A2, PH_SETTLED); a.bgeu(A4, A2, "revert")
    _spill_caller(a)
    _eng_seat_of_caller(a, S2, N)                             # S2 = caller's seat
    _tt_load(a, A4, TT_COMMITTED, S2); a.bne(A4, ZERO, "revert")
    for w in range(8):
        _load_be32(a, T3, S0, 8 + w * 4)
        _tt_key_seatword(a, A3, TT_COMMIT, S2, w); a.sstore(A3, T3)
    _tt_store_imm(a, TT_COMMITTED, S2, 1)
    a.halt()

    # =============================================================== FN_CHECK(tid)
    a.label("eng_check$")
    _eng_action_prologue(a, N)                                # S2 = caller seat == to_act, ACTIVE
    _tt_load(a, A4, TT_STREETBET, S2)
    _ts_load(a, A2, TS_TOCALL); a.bne(A4, A2, "revert")
    _tt_store_imm(a, TT_ACTED, S2, 1)
    a.mv(S3, S2); a.j("eng_advance$")

    # =============================================================== FN_CALL(tid)
    a.label("eng_call$")
    _eng_action_prologue(a, N)
    _ts_load(a, A4, TS_TOCALL)
    _tt_load(a, A2, TT_STREETBET, S2)
    a.bltu(A4, A2, "revert")
    a.sub(A1, A4, A2)
    _eng_commit_chips(a, S2, "eng_call_c")
    _tt_store_imm(a, TT_ACTED, S2, 1)
    a.mv(S3, S2); a.j("eng_advance$")

    # =============================================================== FN_BET(tid, target BE)
    a.label("eng_bet$")
    _eng_action_prologue(a, N)
    _load_be32(a, T6, S0, 8)                                  # T6 = target
    _ts_load(a, A4, TS_TOCALL); a.bgeu(A4, T6, "revert")
    _tt_load(a, A2, TT_STREETBET, S2); a.bgeu(A2, T6, "revert")
    a.sub(A1, T6, A2)                                         # A1 = put
    _tt_load(a, T4, TT_STACK, S2)                             # T4 = stack
    a.li(S3, 0)                                               # S3 = allin flag
    a.bltu(A1, T4, "eng_bet_notallin$"); a.li(S3, 1)
    a.label("eng_bet_notallin$")
    _eng_commit_chips(a, S2, "eng_bet_c")
    _tt_load(a, T4, TT_STREETBET, S2)                         # T4 = reached
    _ts_load(a, A4, TS_TOCALL); _ts_load(a, A5, TS_MINRAISE); a.add(A6, A4, A5)
    a.bltu(T4, A6, "eng_bet_short$")
    a.sub(A5, T4, A4); _ts_store_imm(a, TS_MINRAISE, A5)
    _ts_store_imm(a, TS_TOCALL, T4)
    _eng_clear_acted(a, N)
    _tt_store_imm(a, TT_ACTED, S2, 1)
    a.mv(S3, S2); a.j("eng_advance$")
    a.label("eng_bet_short$")
    a.beq(S3, ZERO, "revert")                                 # under-min only when all-in
    _ts_load(a, A4, TS_TOCALL); a.bgeu(A4, T4, "eng_bet_short_nr$")
    _ts_store_imm(a, TS_TOCALL, T4)
    a.label("eng_bet_short_nr$")
    _tt_store_imm(a, TT_ACTED, S2, 1)
    a.mv(S3, S2); a.j("eng_advance$")

    # =============================================================== FN_FOLD(tid)
    a.label("eng_fold$")
    _eng_action_prologue(a, N)
    _tt_store_imm(a, TT_STATE, S2, ST_FOLDED)
    _tt_store_imm(a, TT_ACTED, S2, 1)
    a.mv(S3, S2); a.j("eng_advance$")

    # =============================================================== _advance(frm=S3)
    a.label("eng_advance$")
    a.li(T1, 0); a.li(T2, -1)
    a.li(T0, 0)
    a.label("eng_adv_nf$")
    a.li(A2, N); a.bgeu(T0, A2, "eng_adv_nf_done$")
    _tt_load(a, A4, TT_STATE, T0)
    a.li(A2, ST_ACTIVE); a.beq(A4, A2, "eng_adv_nf_yes$")
    a.li(A2, ST_ALLIN); a.beq(A4, A2, "eng_adv_nf_yes$")
    a.j("eng_adv_nf_next$")
    a.label("eng_adv_nf_yes$")
    a.addi(T1, T1, 1); a.mv(T2, T0)
    a.label("eng_adv_nf_next$")
    a.addi(T0, T0, 1); a.j("eng_adv_nf$")
    a.label("eng_adv_nf_done$")
    a.li(A2, 1); a.bne(T1, A2, "eng_adv_close_chk$")
    # uncontested
    a.mv(S2, T2)
    _eng_rebuild_pots(a, N)
    a.addi(A4, S2, 1); _ts_store_imm(a, TS_WINBYFOLD, A4)
    a.li(A4, PH_SHOWDOWN); _ts_store_imm(a, TS_PHASE, A4)
    a.halt()
    a.label("eng_adv_close_chk$")
    _ts_load(a, S1, TS_TOCALL)                                # S1 = to_call
    a.li(T1, 1); a.li(T0, 0)
    a.label("eng_adv_am$")
    a.li(A2, N); a.bgeu(T0, A2, "eng_adv_am_done$")
    _tt_load(a, A4, TT_STATE, T0)
    a.li(A2, ST_ACTIVE); a.bne(A4, A2, "eng_adv_am_next$")
    _tt_load(a, A4, TT_ACTED, T0); a.beq(A4, ZERO, "eng_adv_am_no$")
    _tt_load(a, A4, TT_STREETBET, T0); a.bne(A4, S1, "eng_adv_am_no$")
    a.j("eng_adv_am_next$")
    a.label("eng_adv_am_no$")
    a.li(T1, 0); a.j("eng_adv_am_done$")
    a.label("eng_adv_am_next$")
    a.addi(T0, T0, 1); a.j("eng_adv_am$")
    a.label("eng_adv_am_done$")
    a.bne(T1, ZERO, "eng_close_street$")
    # find next ACTIVE seat owing action from frm=S3
    a.mv(T0, S3); a.li(T3, 0)
    a.label("eng_adv_find$")
    a.li(A2, N); a.bgeu(T3, A2, "eng_close_street$")
    a.addi(T0, T0, 1); a.li(A2, N); a.bltu(T0, A2, "eng_adv_find_nm$"); a.sub(T0, T0, A2)
    a.label("eng_adv_find_nm$")
    _tt_load(a, A4, TT_STATE, T0)
    a.li(A2, ST_ACTIVE); a.bne(A4, A2, "eng_adv_find_next$")
    _tt_load(a, A4, TT_ACTED, T0); a.beq(A4, ZERO, "eng_adv_find_hit$")
    _tt_load(a, A4, TT_STREETBET, T0); a.bltu(A4, S1, "eng_adv_find_hit$")
    a.label("eng_adv_find_next$")
    a.addi(T3, T3, 1); a.j("eng_adv_find$")
    a.label("eng_adv_find_hit$")
    _ts_store_imm(a, TS_TOACT, T0)
    _set_deadline(a)
    a.halt()

    # ---------------- _close_street ----------------
    a.label("eng_close_street$")
    _eng_rebuild_pots(a, N)
    a.li(T0, 0)
    a.label("eng_cs_clr$")
    a.li(A2, N); a.bgeu(T0, A2, "eng_cs_clr_done$")
    _tt_store(a, TT_STREETBET, T0, ZERO)
    a.addi(T0, T0, 1); a.j("eng_cs_clr$")
    a.label("eng_cs_clr_done$")
    a.li(T1, 0); a.li(T0, 0)
    a.label("eng_cs_ac$")
    a.li(A2, N); a.bgeu(T0, A2, "eng_cs_ac_done$")
    _tt_load(a, A4, TT_STATE, T0)
    a.li(A2, ST_ACTIVE); a.bne(A4, A2, "eng_cs_ac_next$")
    a.addi(T1, T1, 1)
    a.label("eng_cs_ac_next$")
    a.addi(T0, T0, 1); a.j("eng_cs_ac$")
    a.label("eng_cs_ac_done$")
    _ts_load(a, A4, TS_STREET); a.li(A2, 3); a.bgeu(A4, A2, "eng_cs_showdown$")
    a.li(A2, 2); a.bltu(T1, A2, "eng_cs_showdown$")
    _ts_load(a, A4, TS_STREET); a.addi(A4, A4, 1); _ts_store_imm(a, TS_STREET, A4)
    a.li(A4, 0); _ts_store_imm(a, TS_TOCALL, A4)
    _ts_load(a, A4, TS_BB); _ts_store_imm(a, TS_MINRAISE, A4)
    _eng_clear_acted(a, N)
    _ts_load(a, A5, TS_BUTTON); a.mv(T0, A5); a.li(T3, 0)
    a.label("eng_cs_first$")
    a.li(A2, N); a.bgeu(T3, A2, "eng_cs_showdown$")
    a.addi(T0, T0, 1); a.li(A2, N); a.bltu(T0, A2, "eng_cs_first_nm$"); a.sub(T0, T0, A2)
    a.label("eng_cs_first_nm$")
    _tt_load(a, A4, TT_STATE, T0)
    a.li(A2, ST_ACTIVE); a.beq(A4, A2, "eng_cs_first_hit$")
    a.addi(T3, T3, 1); a.j("eng_cs_first$")
    a.label("eng_cs_first_hit$")
    _ts_store_imm(a, TS_TOACT, T0)
    _set_deadline(a)
    a.halt()
    a.label("eng_cs_showdown$")
    a.li(A4, PH_SHOWDOWN); _ts_store_imm(a, TS_PHASE, A4)
    _set_deadline(a)
    a.halt()

    # =============================================================== FN_REVEAL(tid, c0..c4, nonce32)
    a.label("eng_reveal$")
    _ts_load(a, A4, TS_PHASE); a.li(A2, PH_SHOWDOWN); a.bne(A4, A2, "revert")
    _spill_caller(a)
    _eng_seat_of_caller(a, S2, N)
    _tt_load(a, A4, TT_STATE, S2)
    a.li(A2, ST_FOLDED); a.beq(A4, A2, "revert")
    a.li(A2, ST_EMPTY); a.beq(A4, A2, "revert")
    _tt_load(a, A4, TT_REVEALED, S2); a.bne(A4, ZERO, "revert")
    # build preimage c0..c4 | nonce(32) ; copy cards to SC_CARDS (cards at calldata 8..13, nonce at 13..45)
    a.li(T0, 0)
    a.label("eng_rv_cards$")
    a.li(A2, 5); a.bgeu(T0, A2, "eng_rv_cards_done$")
    a.add(A2, S0, T0); a.lbu(T3, A2, 8)
    a.li(A2, 51); a.bltu(A2, T3, "revert")
    a.li(A2, SC_PRE); a.add(A2, A2, T0); a.sb(T3, A2, 0)
    a.li(A2, SC_CARDS); a.add(A2, A2, T0); a.sb(T3, A2, 0)
    a.addi(T0, T0, 1); a.j("eng_rv_cards$")
    a.label("eng_rv_cards_done$")
    # distinct cards
    a.li(T0, 0)
    a.label("eng_rv_di$")
    a.li(A2, 5); a.bgeu(T0, A2, "eng_rv_di_done$")
    a.addi(T3, T0, 1)
    a.label("eng_rv_dj$")
    a.li(A2, 5); a.bgeu(T3, A2, "eng_rv_dinext$")
    a.li(A2, SC_CARDS); a.add(A2, A2, T0); a.lbu(T4, A2, 0)
    a.li(A2, SC_CARDS); a.add(A2, A2, T3); a.lbu(T5, A2, 0)
    a.beq(T4, T5, "revert")
    a.addi(T3, T3, 1); a.j("eng_rv_dj$")
    a.label("eng_rv_dinext$")
    a.addi(T0, T0, 1); a.j("eng_rv_di$")
    a.label("eng_rv_di_done$")
    # nonce -> SC_PRE+5
    a.li(T0, 0)
    a.label("eng_rv_nonce$")
    a.li(A2, 32); a.bgeu(T0, A2, "eng_rv_hash$")
    a.add(A2, S0, T0); a.lbu(A6, A2, 13)
    a.li(A2, SC_PRE + 5); a.add(A2, A2, T0); a.sb(A6, A2, 0)
    a.addi(T0, T0, 1); a.j("eng_rv_nonce$")
    a.label("eng_rv_hash$")
    a.li(A0, SC_PRE); a.li(A1, 37); a.li(A2, SC_SHA); a.syscall(SYS_SHA256)
    # compare to commit (8 words)
    a.li(T0, 0)
    a.label("eng_rv_cmp$")
    a.li(A2, 8); a.bgeu(T0, A2, "eng_rv_ok$")
    _tt_key_seatword(a, A3, TT_COMMIT, S2, 0); a.add(A3, A3, T0); a.sload_to(T6, A3)
    a.li(A2, SC_SHA); a.slli(A4, T0, 2); a.add(A4, A2, A4)
    a.lbu(A2, A4, 0); a.slli(A2, A2, 8); a.lbu(A6, A4, 1); a.or_(A2, A2, A6); a.slli(A2, A2, 8)
    a.lbu(A6, A4, 2); a.or_(A2, A2, A6); a.slli(A2, A2, 8); a.lbu(A6, A4, 3); a.or_(A2, A2, A6)
    a.bne(T6, A2, "revert")
    a.addi(T0, T0, 1); a.j("eng_rv_cmp$")
    a.label("eng_rv_ok$")
    # store the 5 cards
    a.li(T0, 0)
    a.label("eng_rv_store$")
    a.li(A2, 5); a.bgeu(T0, A2, "eng_rv_store_done$")
    a.li(A2, SC_CARDS); a.add(A2, A2, T0); a.lbu(T3, A2, 0)
    _tt_key_seatword(a, A3, TT_CARD, S2, 0); a.add(A3, A3, T0); a.sstore(A3, T3)
    a.addi(T0, T0, 1); a.j("eng_rv_store$")
    a.label("eng_rv_store_done$")
    a.j("eng_rank5_entry$")
    a.label("eng_rank_back$")
    a.mv(T0, A0)
    _tt_store(a, TT_RANK, S2, T0)
    _tt_store_imm(a, TT_REVEALED, S2, 1)
    _set_deadline(a)
    a.halt()

    # =============================================================== FN_SETTLE(tid)
    a.label("eng_settle$")
    _ts_load(a, A4, TS_PHASE); a.li(A2, PH_SHOWDOWN); a.bne(A4, A2, "revert")
    _ts_load(a, A4, TS_WINBYFOLD); a.bne(A4, ZERO, "eng_settle_run$")
    a.li(T0, 0)
    a.label("eng_set_chk$")
    a.li(A2, N); a.bgeu(T0, A2, "eng_settle_run$")
    _tt_load(a, A4, TT_STATE, T0)
    a.li(A2, ST_ACTIVE); a.beq(A4, A2, "eng_set_chk_need$")
    a.li(A2, ST_ALLIN); a.beq(A4, A2, "eng_set_chk_need$")
    a.j("eng_set_chk_next$")
    a.label("eng_set_chk_need$")
    _tt_load(a, A4, TT_REVEALED, T0); a.beq(A4, ZERO, "revert")
    a.label("eng_set_chk_next$")
    a.addi(T0, T0, 1); a.j("eng_set_chk$")
    a.label("eng_settle_run$")
    a.j("eng_do_settle$")

    # =============================================================== FN_TIMEOUT(tid)
    a.label("eng_timeout$")
    a.number(A4); _ts_load(a, A2, TS_DEADLINE); a.bgeu(A2, A4, "revert")
    _ts_load(a, T2, TS_PHASE)
    a.li(A2, PH_BET); a.beq(T2, A2, "eng_to_bet$")
    a.li(A2, PH_SHOWDOWN); a.beq(T2, A2, "eng_to_showdown$")
    a.j("revert")
    a.label("eng_to_bet$")
    _ts_load(a, S2, TS_TOACT)
    _tt_load(a, A4, TT_STATE, S2)
    a.li(A2, ST_ACTIVE); a.bne(A4, A2, "revert")
    _tt_store_imm(a, TT_STATE, S2, ST_FOLDED)
    _tt_store_imm(a, TT_ACTED, S2, 1)
    a.mv(S3, S2); a.j("eng_advance$")
    a.label("eng_to_showdown$")
    a.li(T0, 0)
    a.label("eng_to_sd_fold$")
    a.li(A2, N); a.bgeu(T0, A2, "eng_to_sd_done$")
    _tt_load(a, A4, TT_STATE, T0)
    a.li(A2, ST_ACTIVE); a.beq(A4, A2, "eng_to_sd_maybe$")
    a.li(A2, ST_ALLIN); a.beq(A4, A2, "eng_to_sd_maybe$")
    a.j("eng_to_sd_next$")
    a.label("eng_to_sd_maybe$")
    _tt_load(a, A4, TT_REVEALED, T0); a.bne(A4, ZERO, "eng_to_sd_next$")
    _tt_store_imm(a, TT_STATE, T0, ST_FOLDED)
    a.label("eng_to_sd_next$")
    a.addi(T0, T0, 1); a.j("eng_to_sd_fold$")
    a.label("eng_to_sd_done$")
    _eng_rebuild_pots(a, N)
    a.j("eng_do_settle$")

    # =============================================================== settlement (multi-way, checked)
    _eng_settle(a, N, max_buyin)

    # ---------------- the 5-card ranker ----------------
    _eng_rank5(a)


# ---------------- §4 engine helpers (tid-namespaced) ----------------
def _eng_next_occupied(a, dst, from_reg, N):
    loop = a._uniq("eno"); hit = a._uniq("eno_hit"); done = a._uniq("eno_done")
    a.mv(T0, from_reg)
    a.li(T6, 0)
    a.label(loop)
    a.li(A2, N); a.bgeu(T6, A2, done)
    a.addi(T0, T0, 1); a.li(A2, N); a.bltu(T0, A2, "%s_nm" % loop); a.li(A2, N); a.sub(T0, T0, A2)
    a.label("%s_nm" % loop)
    _tt_load(a, A4, TT_STATE, T0)
    a.bne(A4, ZERO, hit)
    a.addi(T6, T6, 1); a.j(loop)
    a.label(hit)
    a.label(done)
    a.mv(dst, T0)


def _eng_seat_of_caller(a, dst, N):
    """dst = seat whose stored addr word-6 == caller low-32; revert if none. _spill_caller must have run.
    Uses T0,A2..A6,dst."""
    _caller_low32(a, A6)
    loop = a._uniq("esoc"); hit = a._uniq("esoc_hit"); nxt = a._uniq("esoc_next")
    a.li(T0, 0)
    a.label(loop)
    a.li(A2, N); a.bgeu(T0, A2, "revert")
    _tt_load(a, A4, TT_STATE, T0); a.beq(A4, ZERO, nxt)
    _tt_key_seatword(a, A3, TT_ADDR, T0, 6); a.sload_to(A4, A3)
    a.beq(A4, A6, hit)
    a.label(nxt)
    a.addi(T0, T0, 1); a.j(loop)
    a.label(hit)
    a.mv(dst, T0)


def _eng_action_prologue(a, N):
    _ts_load(a, A4, TS_PHASE); a.li(A2, PH_BET); a.bne(A4, A2, "revert")
    _spill_caller(a)
    _eng_seat_of_caller(a, S2, N)
    _ts_load(a, A4, TS_TOACT); a.bne(A4, S2, "revert")
    _tt_load(a, A4, TT_STATE, S2)
    a.li(A2, ST_ACTIVE); a.bne(A4, A2, "revert")


def _eng_commit_chips(a, seat_reg, tag):
    """Move min(A1, stack) chips from seat stack into street_bet/hand_bet/escrow; all-in if stack hits 0.
    A1 clobbered. seat_reg preserved (S-reg). Guards every accumulator [SEC-C3]."""
    cap = a._uniq(tag + "_cap"); noallin = a._uniq(tag + "_noallin"); have = a._uniq(tag + "_have")
    _tt_load(a, T2, TT_STACK, seat_reg)                       # T2 = stack
    a.bgeu(A1, T2, cap)
    a.mv(T3, A1); a.j(have)
    a.label(cap)
    a.mv(T3, T2)
    a.label(have)
    a.sub(A4, T2, T3); _tt_store(a, TT_STACK, seat_reg, A4)   # stack -= amt
    _tt_load(a, A4, TT_STREETBET, seat_reg); a.add(A5, A4, T3); a.bltu(A5, A4, "revert"); _tt_store(a, TT_STREETBET, seat_reg, A5)
    _tt_load(a, A4, TT_HANDBET, seat_reg); a.add(A5, A4, T3); a.bltu(A5, A4, "revert"); _tt_store(a, TT_HANDBET, seat_reg, A5)
    _ts_load(a, A4, TS_ESCROW); a.add(A5, A4, T3); a.bltu(A5, A4, "revert"); _ts_store_imm(a, TS_ESCROW, A5)
    _tt_load(a, A4, TT_STACK, seat_reg); a.bne(A4, ZERO, noallin)
    _tt_load(a, A4, TT_STATE, seat_reg); a.li(A2, ST_ACTIVE); a.bne(A4, A2, noallin)
    _tt_store_imm(a, TT_STATE, seat_reg, ST_ALLIN)
    a.label(noallin)


def _eng_clear_acted(a, N):
    loop = a._uniq("ecla"); done = a._uniq("ecla_done")
    a.li(T0, 0)
    a.label(loop)
    a.li(A2, N); a.bgeu(T0, A2, done)
    _tt_store(a, TT_ACTED, T0, ZERO)
    a.addi(T0, T0, 1); a.j(loop)
    a.label(done)


def _eng_blinds_for_height(a, N):
    """Set TS_SB / TS_BB from the blind schedule for the CURRENT height [SEC-H2 height->blinds ONLY]. Level
    L = (height - S_START_BLOCK) // level_blocks, clamped to the last level. The schedule is baked into the
    bytecode via a chain of compares. Uses T0..T5,A2..A6."""
    # level index -> T0
    a.number(A4)                                              # A4 = height
    _sload(a, A5, S_START_BLOCK)
    a.bgeu(A4, A5, "ebl_have$")
    a.li(T0, 0); a.j("ebl_set$")                              # before start -> level 0
    a.label("ebl_have$")
    a.sub(A4, A4, A5)                                         # A4 = height - start
    # T0 = A4 // LEVEL_BLOCKS  (constant baked at build via _div_const)
    _div_const(a, T0, A4, _LEVEL_BLOCKS[0])
    a.label("ebl_set$")
    # clamp to NL-1 and select (sb,bb) by a baked compare chain
    _emit_level_select(a, T0)


# the build() captures the level schedule into module-level for the helper; set per build call.
_LEVEL_BLOCKS = [1]
_LEVELS = [(1, 2)]


def _emit_level_select(a, level_reg):
    """Emit a baked chain selecting (sb,bb) for level_reg from _LEVELS, clamped to the last. Stores into
    TS_SB / TS_BB. Uses A2,A4,A6 and level_reg."""
    NL = len(_LEVELS)
    done = a._uniq("els_done")
    for L, (sb, bb) in enumerate(_LEVELS):
        if L < NL - 1:
            nxt = a._uniq("els_nxt")
            a.li(A2, L); a.bne(level_reg, A2, nxt)
            a.li(A4, sb); _ts_store_imm(a, TS_SB, A4)
            a.li(A4, bb); _ts_store_imm(a, TS_BB, A4)
            a.j(done)
            a.label(nxt)
        else:
            # last level: everything >= L gets the last blinds
            a.li(A4, sb); _ts_store_imm(a, TS_SB, A4)
            a.li(A4, bb); _ts_store_imm(a, TS_BB, A4)
    a.label(done)


def _eng_rebuild_pots(a, N):
    """Rebuild layered side pots from per-seat TT_HANDBET (mirrors poker_ref._rebuild_pots, tid-namespaced).
    Writes TT_POTAMT|p / TT_POTELIG|p, persists npots to TS_NPOTS, asserts Σpot == Σhandbet == TS_ESCROW.
    Uses T0..T6,A2..A6,S2,S3. SC_LEVELS word 0 = npots header (per-call)."""
    loop = a._uniq("erp"); inner = a._uniq("erp_in")
    a.li(A2, SC_LEVELS); a.sw(ZERO, A2, 0)
    a.li(S3, 0)
    a.label(loop)
    a.li(T1, 0); a.li(T2, 0); a.li(T0, 0)
    a.label(loop + "_scan")
    a.li(A2, N); a.bgeu(T0, A2, loop + "_scan_done")
    _tt_load(a, A4, TT_HANDBET, T0)
    a.bgeu(S3, A4, loop + "_scan_next")
    a.beq(T1, ZERO, loop + "_take")
    a.bgeu(A4, T2, loop + "_scan_next")
    a.label(loop + "_take")
    a.mv(T2, A4); a.li(T1, 1)
    a.label(loop + "_scan_next")
    a.addi(T0, T0, 1); a.j(loop + "_scan")
    a.label(loop + "_scan_done")
    a.beq(T1, ZERO, loop + "_finish")
    a.sub(T3, T2, S3)
    a.li(T4, 0); a.li(S2, 0); a.li(T0, 0)
    a.label(inner)
    a.li(A2, N); a.bgeu(T0, A2, inner + "_done")
    _tt_load(a, A4, TT_HANDBET, T0)
    a.bltu(A4, T2, inner + "_next")
    a.addi(T4, T4, 1)
    _tt_load(a, A4, TT_STATE, T0)
    a.li(A2, ST_FOLDED); a.beq(A4, A2, inner + "_next")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.or_(S2, S2, A2)
    a.label(inner + "_next")
    a.addi(T0, T0, 1); a.j(inner)
    a.label(inner + "_done")
    a.mulu(A0, T3, T4); a.mv(T3, A0)
    a.mv(S3, T2)
    a.beq(T3, ZERO, loop)
    a.bne(S2, ZERO, loop + "_notorphan")
    a.li(A2, SC_LEVELS); a.lw(T5, A2, 0)
    a.beq(T5, ZERO, "revert")
    a.addi(T6, T5, -1)
    _tt_load(a, A5, TT_POTAMT, T6); a.add(A5, A5, T3); _tt_store(a, TT_POTAMT, T6, A5)
    a.j(loop)
    a.label(loop + "_notorphan")
    a.li(A2, SC_LEVELS); a.lw(T5, A2, 0)
    a.beq(T5, ZERO, loop + "_new")
    a.addi(T6, T5, -1)
    _tt_load(a, A5, TT_POTELIG, T6); a.bne(A5, S2, loop + "_new")
    _tt_load(a, A5, TT_POTAMT, T6); a.add(A5, A5, T3); _tt_store(a, TT_POTAMT, T6, A5)
    a.j(loop)
    a.label(loop + "_new")
    _tt_store(a, TT_POTAMT, T5, T3)
    _tt_store(a, TT_POTELIG, T5, S2)
    a.addi(T5, T5, 1); a.li(A2, SC_LEVELS); a.sw(T5, A2, 0)
    a.j(loop)
    a.label(loop + "_finish")
    a.li(A2, SC_LEVELS); a.lw(T5, A2, 0)
    _ts_store_imm(a, TS_NPOTS, T5)
    a.mv(T0, T5)
    a.label(loop + "_clrstale")
    a.li(A2, 9); a.bgeu(T0, A2, loop + "_clrstale_done")
    _tt_store(a, TT_POTAMT, T0, ZERO)
    _tt_store(a, TT_POTELIG, T0, ZERO)
    a.addi(T0, T0, 1); a.j(loop + "_clrstale")
    a.label(loop + "_clrstale_done")
    a.li(T6, 0); a.li(T0, 0)
    a.label(loop + "_sum")
    a.bgeu(T0, T5, loop + "_sumdone")
    _tt_load(a, A4, TT_POTAMT, T0); a.add(T6, T6, A4)
    a.addi(T0, T0, 1); a.j(loop + "_sum")
    a.label(loop + "_sumdone")
    a.li(T4, 0); a.li(T0, 0)
    a.label(loop + "_hb")
    a.li(A2, N); a.bgeu(T0, A2, loop + "_hbdone")
    _tt_load(a, A4, TT_HANDBET, T0); a.add(T4, T4, A4)
    a.addi(T0, T0, 1); a.j(loop + "_hb")
    a.label(loop + "_hbdone")
    a.bne(T6, T4, "revert")
    _ts_load(a, A4, TS_ESCROW); a.bne(T6, A4, "revert")


def _eng_settle(a, N, buyin):
    """Multi-way showdown settlement (mirrors poker_ref.settle, tid-namespaced). For each pot: winners =
    eligible AND revealed AND max rank (or winbyfold seat); share = pot//len; odd chip to first winner
    clockwise from button. Winnings credited back to the seat's STACK (chips stay in custody — only the
    PRIZE POOL ever leaves custody, on FN_FINALIZE). Asserts Σ credited == TS_ESCROW [SEC-C2] before HALT.
    NO BIS transfer here (tournament chips are internal). Entered by `j eng_do_settle$`."""
    a.label("eng_do_settle$")
    # clear payout scratch (reuse TT_STREETBET)
    a.li(T0, 0)
    a.label("est_clrpay$")
    a.li(A2, N); a.bgeu(T0, A2, "est_clrpay_done$")
    _tt_store(a, TT_STREETBET, T0, ZERO)
    a.addi(T0, T0, 1); a.j("est_clrpay$")
    a.label("est_clrpay_done$")
    _ts_load(a, S3, TS_NPOTS)                                 # S3 = npots
    a.li(S2, 0)
    a.label("est_pot$")
    a.bgeu(S2, S3, "est_paid$")
    _tt_load(a, T6, TT_POTAMT, S2)                            # T6 = pot amount
    _tt_load(a, S1, TT_POTELIG, S2)                           # S1 = elig mask
    _ts_load(a, A4, TS_WINBYFOLD)
    a.beq(A4, ZERO, "est_contested$")
    a.addi(T0, A4, -1)
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.and_(A2, A2, S1)
    a.beq(A2, ZERO, "est_have_winners$")
    a.li(A2, 1); a.sll_r(S1, A2, T0)
    a.j("est_have_winners$")
    a.label("est_contested$")
    a.li(T4, 0); a.li(T1, 0); a.li(T0, 0)
    a.label("est_best$")
    a.li(A2, N); a.bgeu(T0, A2, "est_best_done$")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.and_(A2, A2, S1); a.beq(A2, ZERO, "est_best_next$")
    _tt_load(a, A4, TT_REVEALED, T0); a.beq(A4, ZERO, "est_best_next$")
    _tt_load(a, A4, TT_RANK, T0)
    a.bne(T1, ZERO, "est_best_cmp$")
    a.mv(T4, A4); a.li(T1, 1); a.j("est_best_next$")
    a.label("est_best_cmp$")
    a.bgeu(T4, A4, "est_best_next$"); a.mv(T4, A4)
    a.label("est_best_next$")
    a.addi(T0, T0, 1); a.j("est_best$")
    a.label("est_best_done$")
    a.beq(T1, ZERO, "revert")
    a.li(T5, 0); a.li(T0, 0)
    a.label("est_wm$")
    a.li(A2, N); a.bgeu(T0, A2, "est_wm_done$")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.and_(A2, A2, S1); a.beq(A2, ZERO, "est_wm_next$")
    _tt_load(a, A4, TT_REVEALED, T0); a.beq(A4, ZERO, "est_wm_next$")
    _tt_load(a, A4, TT_RANK, T0); a.bne(A4, T4, "est_wm_next$")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.or_(T5, T5, A2)
    a.label("est_wm_next$")
    a.addi(T0, T0, 1); a.j("est_wm$")
    a.label("est_wm_done$")
    a.mv(S1, T5)
    a.label("est_have_winners$")
    a.li(T1, 0); a.li(T0, 0)
    a.label("est_pc$")
    a.li(A2, N); a.bgeu(T0, A2, "est_pc_done$")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.and_(A2, A2, S1); a.beq(A2, ZERO, "est_pc_next$")
    a.addi(T1, T1, 1)
    a.label("est_pc_next$")
    a.addi(T0, T0, 1); a.j("est_pc$")
    a.label("est_pc_done$")
    a.beq(T1, ZERO, "revert")
    a.divu(T2, T3, T6, T1, tmp_i=T4, tmp_bit=T5)
    a.mulu(A0, T2, T1, tmp_shift=T4, tmp_mcand=T5)
    a.sub(T3, T6, A0)                                         # T3 = odd
    a.li(T0, 0)
    a.label("est_dist$")
    a.li(A2, N); a.bgeu(T0, A2, "est_dist_done$")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.and_(A2, A2, S1); a.beq(A2, ZERO, "est_dist_next$")
    _tt_load(a, A4, TT_STREETBET, T0); a.add(A4, A4, T2); _tt_store(a, TT_STREETBET, T0, A4)
    a.label("est_dist_next$")
    a.addi(T0, T0, 1); a.j("est_dist$")
    a.label("est_dist_done$")
    a.beq(T3, ZERO, "est_pot_next$")
    _ts_load(a, T4, TS_BUTTON)
    a.li(T1, 0xFFFFFFFF); a.li(T2, -1); a.li(T0, 0)
    a.label("est_odd$")
    a.li(A2, N); a.bgeu(T0, A2, "est_odd_done$")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.and_(A2, A2, S1); a.beq(A2, ZERO, "est_odd_next$")
    a.sub(A4, T0, T4); a.addi(A4, A4, -1)
    _eng_mod_n(a, A4, N)
    a.bgeu(A4, T1, "est_odd_next$")
    a.mv(T1, A4); a.mv(T2, T0)
    a.label("est_odd_next$")
    a.addi(T0, T0, 1); a.j("est_odd$")
    a.label("est_odd_done$")
    _tt_load(a, A4, TT_STREETBET, T2); a.add(A4, A4, T3); _tt_store(a, TT_STREETBET, T2, A4)
    a.label("est_pot_next$")
    a.addi(S2, S2, 1); a.j("est_pot$")
    a.label("est_paid$")
    # closing invariant: Σ credited (TT_STREETBET) == TS_ESCROW [SEC-C2]
    a.li(T6, 0); a.li(T0, 0)
    a.label("est_inv$")
    a.li(A2, N); a.bgeu(T0, A2, "est_inv_done$")
    _tt_load(a, A4, TT_STREETBET, T0); a.add(T6, T6, A4)
    a.addi(T0, T0, 1); a.j("est_inv$")
    a.label("est_inv_done$")
    _ts_load(a, A4, TS_ESCROW); a.bne(T6, A4, "revert")
    # mark settled (idempotent terminal for the hand) BEFORE crediting
    a.li(A4, PH_SETTLED); _ts_store_imm(a, TS_PHASE, A4)
    # credit winnings back to STACKS (chips stay in custody). stack += payout.
    a.li(T0, 0)
    a.label("est_credit$")
    a.li(A2, N); a.bgeu(T0, A2, "est_credit_done$")
    _tt_load(a, S1, TT_STREETBET, T0); a.beq(S1, ZERO, "est_credit_next$")
    _tt_load(a, A4, TT_STACK, T0); a.add(A4, A4, S1); _tt_store(a, TT_STACK, T0, A4)
    a.label("est_credit_next$")
    a.addi(T0, T0, 1); a.j("est_credit$")
    a.label("est_credit_done$")
    a.halt()


def _eng_mod_n(a, reg, N):
    addl = a._uniq("emodn_add"); subl = a._uniq("emodn_sub")
    a.label(addl)
    a.li(A5, 0x80000000); a.and_(A6, reg, A5); a.beq(A6, ZERO, subl)
    a.li(A5, N); a.add(reg, reg, A5); a.j(addl)
    a.label(subl)
    a.li(A5, N); a.bltu(reg, A5, "%s_done" % subl); a.sub(reg, reg, A5); a.j(subl)
    a.label("%s_done" % subl)


# =================================================================== the 5-card ranker (verbatim from poker.py)
def _eng_rank5(a):
    """Rank SC_CARDS[0..4] -> A0 = category<<20 | tiebreak. Entered by `j eng_rank5_entry$`; returns by `j
    eng_rank_back$`. Uses ONLY T0..T6 + A0/A2..A6."""
    a.label("eng_rank5_entry$")
    a.li(T0, 0)
    a.label("er5_zc$")
    a.li(A2, 13); a.bgeu(T0, A2, "er5_zc_done$")
    a.li(A2, SC_CNT); a.add(A2, A2, T0); a.sb(ZERO, A2, 0)
    a.addi(T0, T0, 1); a.j("er5_zc$")
    a.label("er5_zc_done$")
    a.li(T0, 0); a.li(T1, 1); a.li(T2, -1)
    a.label("er5_scan$")
    a.li(A2, 5); a.bgeu(T0, A2, "er5_counts$")
    a.li(A2, SC_CARDS); a.add(A2, A2, T0); a.lbu(A4, A2, 0)
    a.li(A5, 0); a.mv(A6, A4)
    a.label("er5_div$")
    a.li(A2, 13); a.bltu(A6, A2, "er5_divdone$")
    a.addi(A6, A6, -13); a.addi(A5, A5, 1); a.j("er5_div$")
    a.label("er5_divdone$")
    a.li(A2, SC_CNT); a.add(A2, A2, A6); a.lbu(A3, A2, 0); a.addi(A3, A3, 1); a.sb(A3, A2, 0)
    a.li(A2, -1); a.bne(T2, A2, "er5_sc_cmp$")
    a.mv(T2, A5); a.j("er5_sc_next$")
    a.label("er5_sc_cmp$")
    a.beq(T2, A5, "er5_sc_next$"); a.li(T1, 0)
    a.label("er5_sc_next$")
    a.addi(T0, T0, 1); a.j("er5_scan$")
    a.label("er5_counts$")
    a.li(T0, 0); a.li(T2, 0); a.li(T3, 0); a.li(T4, 0)
    a.label("er5_cc$")
    a.li(A2, 13); a.bgeu(T0, A2, "er5_straight$")
    a.li(A2, SC_CNT); a.add(A2, A2, T0); a.lbu(A4, A2, 0)
    a.li(A2, 2); a.beq(A4, A2, "er5_cc2$")
    a.li(A2, 3); a.beq(A4, A2, "er5_cc3$")
    a.li(A2, 4); a.beq(A4, A2, "er5_cc4$")
    a.j("er5_cc_next$")
    a.label("er5_cc2$"); a.addi(T2, T2, 1); a.j("er5_cc_next$")
    a.label("er5_cc3$"); a.addi(T3, T3, 1); a.j("er5_cc_next$")
    a.label("er5_cc4$"); a.addi(T4, T4, 1)
    a.label("er5_cc_next$")
    a.addi(T0, T0, 1); a.j("er5_cc$")
    a.label("er5_straight$")
    a.li(T5, -1)
    a.li(T0, 0)
    a.label("er5_st$")
    a.li(A2, 8); a.bltu(A2, T0, "er5_st_wheel$")
    a.li(T6, 0); a.li(A5, 1)
    a.label("er5_st_k$")
    a.li(A2, 5); a.bgeu(T6, A2, "er5_st_chk$")
    a.li(A2, SC_CNT); a.add(A2, A2, T0); a.add(A2, A2, T6); a.lbu(A4, A2, 0)
    a.li(A2, 1); a.beq(A4, A2, "er5_st_kok$"); a.li(A5, 0)
    a.label("er5_st_kok$")
    a.addi(T6, T6, 1); a.j("er5_st_k$")
    a.label("er5_st_chk$")
    a.beq(A5, ZERO, "er5_st_next$")
    a.addi(T5, T0, 4)
    a.label("er5_st_next$")
    a.addi(T0, T0, 1); a.j("er5_st$")
    a.label("er5_st_wheel$")
    a.li(A2, -1); a.bne(T5, A2, "er5_cat$")
    a.li(A2, SC_CNT); a.lbu(A4, A2, 12); a.li(A3, 1); a.bne(A4, A3, "er5_cat$")
    a.lbu(A4, A2, 0); a.bne(A4, A3, "er5_cat$")
    a.lbu(A4, A2, 1); a.bne(A4, A3, "er5_cat$")
    a.lbu(A4, A2, 2); a.bne(A4, A3, "er5_cat$")
    a.lbu(A4, A2, 3); a.bne(A4, A3, "er5_cat$")
    a.li(T5, 3)
    a.label("er5_cat$")
    a.li(A0, 0)
    a.li(A2, -1); a.beq(T5, A2, "er5_nostr$")
    a.beq(T1, ZERO, "er5_str$")
    a.li(A0, 8); a.j("er5_tb$")
    a.label("er5_str$"); a.li(A0, 4); a.j("er5_tb$")
    a.label("er5_nostr$")
    a.bne(T4, ZERO, "er5_quads$")
    a.beq(T3, ZERO, "er5_no3$")
    a.beq(T2, ZERO, "er5_trips$")
    a.li(A0, 6); a.j("er5_tb$")
    a.label("er5_trips$"); a.li(A0, 3); a.j("er5_tb$")
    a.label("er5_no3$")
    a.beq(T1, ZERO, "er5_pairs$")
    a.li(A0, 5); a.j("er5_tb$")
    a.label("er5_pairs$")
    a.li(A2, 2); a.beq(T2, A2, "er5_2pair$")
    a.li(A2, 1); a.beq(T2, A2, "er5_1pair$")
    a.li(A0, 0); a.j("er5_tb$")
    a.label("er5_2pair$"); a.li(A0, 2); a.j("er5_tb$")
    a.label("er5_1pair$"); a.li(A0, 1); a.j("er5_tb$")
    a.label("er5_quads$"); a.li(A0, 7)
    a.label("er5_tb$")
    a.slli(A0, A0, 20)
    a.li(A2, -1); a.beq(T5, A2, "er5_tb_counts$")
    a.or_(A0, A0, T5); a.j("eng_rank_back$")
    a.label("er5_tb_counts$")
    a.li(T6, 0)
    a.li(T5, 4)
    a.label("er5_tb_c$")
    a.beq(T5, ZERO, "er5_tb_done$")
    a.li(T0, 12)
    a.label("er5_tb_r$")
    a.li(A2, SC_CNT); a.add(A2, A2, T0); a.lbu(A4, A2, 0)
    a.bne(A4, T5, "er5_tb_rnext$")
    a.slli(T6, T6, 4); a.or_(T6, T6, T0)
    a.label("er5_tb_rnext$")
    a.beq(T0, ZERO, "er5_tb_cnext$")
    a.addi(T0, T0, -1); a.j("er5_tb_r$")
    a.label("er5_tb_cnext$")
    a.addi(T5, T5, -1); a.j("er5_tb_c$")
    a.label("er5_tb_done$")
    a.or_(A0, A0, T6)
    a.j("eng_rank_back$")
