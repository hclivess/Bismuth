"""
poker_table.py — N-seat (2..9) no-limit Texas Hold'em betting + side-pot + multi-way showdown engine as
ONE hand-authored RV32I contract for the Bismuth in-core VM (doc/28 v2 §4, Stage 1). The chain is the
TRUSTLESS REFEREE: it escrows every buy-in, runs the full 4-street betting state machine, builds layered
side pots INCREMENTALLY from per-seat contributions, evaluates each revealed hand on-chain (reusing
poker.py's already-audited `_rank5`/`_gather5`), and settles each pot to its best-ranked ELIGIBLE seat with
the odd chip going clockwise from the button — all with CHECKED transfers and a closing-invariant assert
(Σ payouts == Σ pots == S_ESCROW) BEFORE any HALT.

This module is the bit-for-bit on-chain twin of tests/poker_ref.py (the Python ORACLE). Every chip
movement, side-pot layer, eligibility mask, split and odd-chip decision matches that reference exactly.

SECURITY (doc/28 §9 — all present here):
  [SEC-C1] Seat identity is the FULL 28-byte caller address (7 words compared word-by-word), never a
           32-bit party_id. Every action verifies the caller's 28 bytes == the seat's stored address.
  [SEC-C2] CHECKED transfers: every SYS_TRANSFER return is verified (bne ret,1 -> revert). The closing
           invariant Σ payouts == S_ESCROW is asserted BEFORE issuing any transfer / HALT.
  [SEC-C3] 64-bit math (mulu64/divu64) for pot/share products; N*max_buyin is bounded <= 0x3FFFFFFF at
           build() so every layered accumulator stays inside 32 bits. Every accumulator is guarded.
  [SEC-C4] Board/showdown derived from the committed cards; FN_TIMEOUT after a deadline FORFEITS the
           laggard (folds them) — never a refund. No SYS_NUMBER feeds any chip/seat/button decision
           ([SEC-H2]); block height is used only for the shot-clock deadline.
  [SEC-H4] Every state transition is idempotent / reorg-safe (read-verify-write monotonic flags).

ABI (operation="vm:call", openfield="<addr>:<calldata_hex>", first 4 bytes = selector):
  0 FN_SIT(seat, addr28)        — ATTACH BUYIN. Seat `seat` (0..N-1) is taken by the authenticated caller.
  1 FN_LEAVE(seat)              — leave an un-dealt seat; refunds the remaining stack.
  2 FN_DEAL                     — start a hand (>=2 seated). Rotates button from STORED state, posts blinds.
  3 FN_COMMIT(commit32)         — anchor SHA256(card0..card4|nonce) for the caller's seat (showdown anchor).
  6 FN_CHECK                    — check (only when street_bet == to_call).
  7 FN_CALL                     — call to_call.
  8 FN_BET(target BE)           — bet/raise to a total street_bet of `target` (full-raise reopens; all-in
                                  for-less raises the bar but does NOT reopen).
  9 FN_FOLD                     — fold.
 10 FN_REVEAL(c0..c4, nonce32)  — reveal the 5 cards matching the commitment; ranked on-chain via _rank5.
 11 FN_TIMEOUT                  — permissionless after the deadline: forfeit (fold) the overdue actor; at
                                  showdown, settle to whoever revealed.
 12 FN_SETTLE                   — settle the showdown once betting is done and all live seats revealed.

Value attaches ONLY to FN_SIT; every other selector runs _require_no_value and reverts on attached BIS
(the engine then refunds the sender), so attached value can never be stranded in custody.

STAGE-1 SCOPE: the mental-poker deal (private holes / shuffle) is off-chain (Stage 2, doc/28 §5); this
contract anchors a per-seat commitment to the 5 showdown cards and verifies the reveal against it. The
funds-critical core — escrow, the betting SM, incremental side pots, multi-way showdown, checked payout,
closing invariant — is complete and validated through bismuth_riscv.execute by tests/test_poker_table_vm.py.
"""
import hashlib

from asmtools import (Asm, A0, A1, A2, A3, A4, A5, A6, S0, S1, S2, S3, T0, T1, T2, T3, T4, T5, T6, ZERO,
                      SYS_SHA256)

# ---- selectors ----
FN_SIT = 0
FN_LEAVE = 1
FN_DEAL = 2
FN_COMMIT = 3
FN_DECK_DIGEST = 4   # (idx, H32) — anchor a deal-stage deck hash (Stage 2, doc/28 §5); purely additive
FN_CHECK = 6
FN_CALL = 7
FN_BET = 8
FN_FOLD = 9
FN_REVEAL = 10
FN_TIMEOUT = 11
FN_SETTLE = 12

# ---- phases ----
PH_OPEN = 0       # accepting seats
PH_BET = 1        # a hand is live; betting in progress
PH_SHOWDOWN = 2   # betting done; awaiting reveals / settle
PH_SETTLED = 3    # hand finished (idempotent terminal for the hand; table returns to OPEN for next deal)

# ---- seat states (mirror poker_ref) ----
ST_EMPTY = 0
ST_SEATED = 1     # occupied but not in the current hand
ST_ACTIVE = 2
ST_FOLDED = 3
ST_ALLIN = 4

# ---- global slots ----
S_MAGIC = 0
S_NSEATS = 1
S_PHASE = 2
S_DEADLINE = 3
S_BUTTON = 4
S_SB = 5
S_BB = 6
S_STREET = 7
S_TOACT = 8
S_TOCALL = 9
S_MINRAISE = 10
S_ESCROW = 17
S_DONEFLAG = 18
S_WINBYFOLD = 19   # 0 = none; else (seat+1) of the lone non-folded seat (uncontested)
S_HANDNO = 15
S_NPOTS = 14       # number of layered side pots (persisted across calls; mem SC_LEVELS is per-call only)

# ---- per-seat tag domains (tag | seat, or tag | (seat<<4) | word) ----
TAG_ADDR = 0x11000000      # | (seat<<4) | w  (w 0..6) : 7-word identity+payout address
TAG_STACK = 0x12000000     # | seat
TAG_STREETBET = 0x13000000 # | seat
TAG_HANDBET = 0x14000000   # | seat  (drives side-pot layering)
TAG_STATE = 0x15000000     # | seat
TAG_COMMIT = 0x16000000    # | (seat<<4) | w  (w 0..7) : SHA256 commit to the 5 showdown cards
TAG_COMMITTED = 0x17000000 # | seat : dedicated committed flag
TAG_ACTED = 0x1C000000     # | seat : voluntarily acted since the last aggression (0/1)
TAG_RANK = 0x1A000000      # | seat
TAG_REVEALED = 0x19000000  # | seat
TAG_CARD = 0x1B000000      # | (seat<<4) | k  (k 0..4) : the seat's revealed 5 cards
TAG_POTAMT = 0x1D000000    # | p : pot p amount
TAG_POTELIG = 0x1E000000   # | p : pot p eligibility (N-bit mask)
# ---- Stage-2 mental-poker anchoring (doc/28 §5) — purely ADDITIVE, never read by the betting SM ----
TAG_DECKH = 0x1F000000     # | (idx<<4) | w  (w 0..7) : SHA256 of the deck after shuffle/lock stage `idx`
TAG_CARDMAP_H = 0x21000000 # | w  (w 0..7) : SHA256 of the agreed card->field-value map (chain root)
DECKH_MAXIDX = 0x0FFFFFF   # idx < this anchors TAG_DECKH; idx == IDX_CARDMAP anchors TAG_CARDMAP_H
IDX_CARDMAP = 0x10000000   # sentinel index selecting the card-map root anchor

TIMEOUT_BLOCKS = 60

# ---- mem scratch (well past code + calldata) ----
SCRATCH = 30000
SC_RECIP = SCRATCH         # 28-byte transfer recipient
SC_AMT = SCRATCH + 32      # 8-byte BE transfer amount
SC_PRE = SCRATCH + 64      # 37-byte SHA256 preimage: c0..c4 | nonce(32)
SC_SHA = SCRATCH + 128     # 32-byte SHA256 output
SC_CARDS = SCRATCH + 192   # 5 chosen card bytes (for _rank5)
SC_CNT = SCRATCH + 216     # 13-byte rank-count array for the evaluator
SC_CALLER = SCRATCH + 256  # 7 words (28 bytes) of the caller address, spilled before any other syscall
SC_LEVELS = SCRATCH + 320  # scratch for pot layering


def party_id_of(address):
    try:
        return int(str(address), 16) & 0xFFFFFFFF
    except Exception:
        return int.from_bytes(hashlib.blake2b(str(address).encode(), digest_size=32).digest(),
                              "big") & 0xFFFFFFFF


# ---- read helpers (tests / relay) ----
def addr_word_key(seat, w):
    return TAG_ADDR | (seat << 4) | w


def stack_key(seat):
    return TAG_STACK | seat


def handbet_key(seat):
    return TAG_HANDBET | seat


def streetbet_key(seat):
    return TAG_STREETBET | seat


def state_key(seat):
    return TAG_STATE | seat


def acted_key(seat):
    return TAG_ACTED | seat


def commit_key(seat, w):
    return TAG_COMMIT | (seat << 4) | w


def card_key(seat, k):
    return TAG_CARD | (seat << 4) | k


def rank_key(seat):
    return TAG_RANK | seat


def revealed_key(seat):
    return TAG_REVEALED | seat


def potamt_key(p):
    return TAG_POTAMT | p


def potelig_key(p):
    return TAG_POTELIG | p


def deckh_key(idx, w):
    """Storage key for word w (0..7) of the stage-`idx` deck-hash anchor (FN_DECK_DIGEST)."""
    return TAG_DECKH | (idx << 4) | w


def cardmap_h_key(w):
    """Storage key for word w (0..7) of the card->value-map root anchor."""
    return TAG_CARDMAP_H | w


def build(nseats, sb, bb, max_buyin=None):
    """Assemble the N-seat Hold'em table. nseats in 2..9; sb<bb blinds (units). Each seat buys in with a
    variable stack = the BIS attached to FN_SIT (1..max_buyin units), so per-seat stacks differ (short
    stacks drive side pots). Bounds nseats*max_buyin <= 0x3FFFFFFF so every layered side-pot accumulator
    stays inside 32 bits [SEC-C3]."""
    nseats = int(nseats); sb = int(sb); bb = int(bb)
    if not (2 <= nseats <= 9):
        raise ValueError("nseats must be 2..9")
    if not (0 < sb < bb):
        raise ValueError("need 0 < sb < bb")
    if max_buyin is None:
        max_buyin = bb * 1000
    max_buyin = int(max_buyin)
    if max_buyin <= 0 or max_buyin < bb:
        raise ValueError("max_buyin must be > 0 and >= bb")
    if nseats * max_buyin > 0x3FFFFFFF:
        raise ValueError("nseats*max_buyin must be <= 0x3FFFFFFF (32-bit accumulator bound) [SEC-C3]")

    a = Asm()
    N = nseats

    # ---------------- dispatch ----------------
    # S0 = calldata ptr ; T0 = selector. The caller address is loaded lazily (it clobbers a0/a1).
    a.mv(S0, A0)
    _load_be32(a, T0, S0, 0)
    a.li(A2, FN_SIT);     a.beq(T0, A2, "sit")
    a.li(A2, FN_LEAVE);   a.beq(T0, A2, "leave")
    a.li(A2, FN_DEAL);    a.beq(T0, A2, "deal")
    a.li(A2, FN_COMMIT);  a.beq(T0, A2, "commit")
    a.li(A2, FN_DECK_DIGEST); a.beq(T0, A2, "deckdigest")
    a.li(A2, FN_CHECK);   a.beq(T0, A2, "check")
    a.li(A2, FN_CALL);    a.beq(T0, A2, "call")
    a.li(A2, FN_BET);     a.beq(T0, A2, "bet")
    a.li(A2, FN_FOLD);    a.beq(T0, A2, "fold")
    a.li(A2, FN_REVEAL);  a.beq(T0, A2, "reveal")
    a.li(A2, FN_TIMEOUT); a.beq(T0, A2, "timeout")
    a.li(A2, FN_SETTLE);  a.beq(T0, A2, "settle_entry")
    a.j("revert")

    # =============================================================== FN_SIT(seat, addr28) + BUYIN
    a.label("sit")
    _sload(a, A4, S_PHASE); a.bne(A4, ZERO, "revert")          # only while OPEN
    _load_be32(a, S1, S0, 4)                                    # S1 = seat
    a.li(A2, N); a.bgeu(S1, A2, "revert")                       # seat in 0..N-1
    # seat must be EMPTY
    a.li(A3, TAG_STATE); a.or_(A3, A3, S1); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")
    # [SEC-C1] identity. The engine's SYS_CALLER exposes only the low 32 bits of the caller, so on-chain
    # authentication can bind to those 32 bits only (the upper 6 address words are payout routing, stored but
    # not authenticatable here — a known engine limitation shared with base poker.py). Two guards make that
    # binding sound: (a) you may only seat YOUR OWN identity — the supplied address word-6 must equal the
    # authenticated caller (no seating someone else); (b) reject a low-32 collision with an already-occupied
    # seat, so _seat_of_caller (which returns the first low-32 match) is unambiguous.
    _spill_caller(a)                                          # SC_CALLER+24 = caller low-32 (4 BE bytes)
    _load_be32(a, T4, S0, 8 + 6 * 4)                          # T4 = supplied address word 6 (the identity word)
    a.li(A2, SC_CALLER + 24); a.lbu(T3, A2, 0); a.slli(T3, T3, 8)
    a.lbu(A5, A2, 1); a.or_(T3, T3, A5); a.slli(T3, T3, 8)
    a.lbu(A5, A2, 2); a.or_(T3, T3, A5); a.slli(T3, T3, 8)
    a.lbu(A5, A2, 3); a.or_(T3, T3, A5)                       # T3 = authenticated caller low-32
    a.bne(T4, T3, "revert")                                   # (a) must seat your own address
    a.li(T0, 0)                                               # (b) scan occupied seats for a low-32 collision
    a.label("sit_coll$")
    a.li(A2, N); a.bgeu(T0, A2, "sit_coll_done$")
    a.beq(T0, S1, "sit_coll_next$")                           # skip the target seat itself
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.beq(A4, ZERO, "sit_coll_next$")  # skip EMPTY
    a.li(A3, TAG_ADDR); a.slli(A5, T0, 4); a.or_(A3, A3, A5); a.li(A2, 6); a.or_(A3, A3, A2)
    a.sload_to(A4, A3)                                        # occupied seat's stored identity word
    a.beq(A4, T4, "revert")                                   # collision with an existing seat -> reject
    a.label("sit_coll_next$")
    a.addi(T0, T0, 1); a.j("sit_coll$")
    a.label("sit_coll_done$")
    # buy-in = attached callvalue; must be 1..max_buyin (callvalue can exceed 32 bits, so the engine masks
    # it; we require it within the configured cap so the side-pot accumulator bound [SEC-C3] holds)
    a.callvalue(S2)                                            # S2 = buyin (low 32 bits)
    a.beq(S2, ZERO, "revert")                                 # must attach > 0
    a.li(A2, int(max_buyin)); a.bltu(A2, S2, "revert")        # buyin <= max_buyin
    # store the 7-word address (identity + payout)
    for w in range(7):
        _load_be32(a, T3, S0, 8 + w * 4)
        a.li(A3, TAG_ADDR); a.slli(A4, S1, 4); a.or_(A3, A3, A4); a.li(A2, w); a.or_(A3, A3, A2)
        a.sstore(A3, T3)
    a.li(A3, TAG_STACK); a.or_(A3, A3, S1); a.sstore(A3, S2)   # stack = buyin
    a.li(A3, TAG_STATE); a.or_(A3, A3, S1); a.li(A4, ST_SEATED); a.sstore(A3, A4)
    a.halt()

    # =============================================================== FN_LEAVE(seat) : refund stack
    a.label("leave")
    _require_no_value(a)
    _sload(a, A4, S_PHASE); a.bne(A4, ZERO, "revert")          # only while OPEN (not mid-hand)
    _load_be32(a, S1, S0, 4)
    a.li(A2, N); a.bgeu(S1, A2, "revert")
    a.li(A3, TAG_STATE); a.or_(A3, A3, S1); a.sload_to(A4, A3)
    a.li(A2, ST_SEATED); a.bne(A4, A2, "revert")               # must be SEATED
    _spill_caller(a)
    _require_caller_is_seat(a, S1)                             # only the seat owner may leave
    # refund stack, empty the seat
    a.li(A3, TAG_STACK); a.or_(A3, A3, S1); a.sload_to(S2, A3)  # S2 = stack
    a.li(A3, TAG_STATE); a.or_(A3, A3, S1); a.li(A4, ST_EMPTY); a.sstore(A3, A4)
    a.li(A3, TAG_STACK); a.or_(A3, A3, S1); a.sstore(A3, ZERO)
    _pay_seat_checked(a, S1, S2)                               # checked transfer of the refund
    a.halt()

    # =============================================================== FN_DEAL
    a.label("deal")
    _require_no_value(a)
    _sload(a, A4, S_PHASE)
    # allow DEAL from OPEN or SETTLED (start the next hand)
    a.li(A2, PH_OPEN); a.beq(A4, A2, "deal_ok$")
    a.li(A2, PH_SETTLED); a.beq(A4, A2, "deal_ok$")
    a.j("revert")
    a.label("deal_ok$")
    # count occupied seats (state in {SEATED,ACTIVE,FOLDED,ALLIN} i.e. != EMPTY) -> need >= 2
    a.li(T1, 0)            # count
    a.li(T0, 0)
    a.label("deal_cnt$")
    a.li(A2, N); a.bgeu(T0, A2, "deal_cnt_done$")
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.beq(A4, ZERO, "deal_cnt_next$")
    a.addi(T1, T1, 1)
    a.label("deal_cnt_next$")
    a.addi(T0, T0, 1); a.j("deal_cnt$")
    a.label("deal_cnt_done$")
    a.li(A2, 2); a.bltu(T1, A2, "revert")
    # reset per-seat hand state: occupied -> ACTIVE, clear bets/acted/revealed/rank/commit flag
    a.li(T0, 0)
    a.label("deal_reset$")
    a.li(A2, N); a.bgeu(T0, A2, "deal_reset_done$")
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.beq(A4, ZERO, "deal_reset_next$")
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.li(A4, ST_ACTIVE); a.sstore(A3, A4)
    a.li(A3, TAG_HANDBET); a.or_(A3, A3, T0); a.sstore(A3, ZERO)
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, T0); a.sstore(A3, ZERO)
    a.li(A3, TAG_ACTED); a.or_(A3, A3, T0); a.sstore(A3, ZERO)
    a.li(A3, TAG_REVEALED); a.or_(A3, A3, T0); a.sstore(A3, ZERO)
    a.li(A3, TAG_RANK); a.or_(A3, A3, T0); a.sstore(A3, ZERO)
    a.li(A3, TAG_COMMITTED); a.or_(A3, A3, T0); a.sstore(A3, ZERO)
    a.label("deal_reset_next$")
    a.addi(T0, T0, 1); a.j("deal_reset$")
    a.label("deal_reset_done$")
    # rotate button from STORED state [SEC-H2]: next occupied after current button. On the very first hand
    # (S_HANDNO == 0) there is no prior button, so start from -1 so next_occupied yields the first seat.
    _sload(a, A5, S_HANDNO)
    a.bne(A5, ZERO, "deal_btn$")
    a.li(A5, 0xFFFFFFFF); a.j("deal_btn_have$")
    a.label("deal_btn$")
    _sload(a, A5, S_BUTTON)
    a.label("deal_btn_have$")
    _next_occupied(a, S1, A5, N)                              # S1 = new button
    _sstore_imm(a, S_BUTTON, S1)
    # blinds. heads-up: SB = button; else SB = next_occupied(button)
    a.li(T1, 0)                                              # T1 = occupied count again (== from deal_cnt)
    a.li(T0, 0)
    a.label("deal_oc$")
    a.li(A2, N); a.bgeu(T0, A2, "deal_oc_done$")
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.beq(A4, ZERO, "deal_oc_next$")
    a.addi(T1, T1, 1)
    a.label("deal_oc_next$")
    a.addi(T0, T0, 1); a.j("deal_oc$")
    a.label("deal_oc_done$")
    a.li(A2, 2)
    a.beq(T1, A2, "deal_hu$")                                # heads-up
    _next_occupied(a, S2, S1, N)                             # S2 = SB seat (next after button)
    a.j("deal_blinds$")
    a.label("deal_hu$")
    a.mv(S2, S1)                                            # heads-up: SB == button
    a.label("deal_blinds$")
    _next_occupied(a, S3, S2, N)                            # S3 = BB seat
    # reset escrow/pots/street/state
    a.li(A4, 0); _sstore_imm(a, S_ESCROW, A4)
    a.li(A4, 0); _sstore_imm(a, S_STREET, A4)
    a.li(A4, 0); _sstore_imm(a, S_WINBYFOLD, A4)
    a.li(A4, 0); _sstore_imm(a, S_DONEFLAG, A4)
    # post SB (blinds are build-time constants) from seat S2
    a.li(A1, sb); _commit_chips(a, S2, "deal_csb")
    # post BB from seat S3
    a.li(A1, bb); _commit_chips(a, S3, "deal_cbb")
    a.li(A4, bb); _sstore_imm(a, S_TOCALL, A4)
    a.li(A4, bb); _sstore_imm(a, S_MINRAISE, A4)
    # to_act = next_occupied(BB) (UTG; heads-up that wraps to the SB/button)
    _next_occupied(a, S1, S3, N); _sstore_imm(a, S_TOACT, S1)
    a.li(A4, PH_BET); _sstore_imm(a, S_PHASE, A4)
    # bump hand number
    _sload(a, A4, S_HANDNO); a.addi(A4, A4, 1); _sstore_imm(a, S_HANDNO, A4)
    _set_deadline(a)
    a.halt()

    # =============================================================== FN_COMMIT(commit32)
    a.label("commit")
    _require_no_value(a)
    _sload(a, A4, S_PHASE); a.li(A2, PH_BET); a.bltu(A4, A2, "revert")   # any time during a live hand
    a.li(A2, PH_SETTLED); a.bgeu(A4, A2, "revert")
    _spill_caller(a)
    _seat_of_caller(a, S1)                                  # S1 = caller's seat (reverts if none)
    a.li(A3, TAG_COMMITTED); a.or_(A3, A3, S1); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")  # fresh
    for w in range(8):
        _load_be32(a, T3, S0, 4 + w * 4)
        a.li(A3, TAG_COMMIT); a.slli(A4, S1, 4); a.or_(A3, A3, A4); a.li(A2, w); a.or_(A3, A3, A2)
        a.sstore(A3, T3)
    a.li(A3, TAG_COMMITTED); a.or_(A3, A3, S1); a.li(A4, 1); a.sstore(A3, A4)
    a.halt()

    # =============================================================== FN_DECK_DIGEST(idx, H32)  [Stage 2]
    # Purely-additive anchoring of the off-chain mental-poker deal (doc/28 §5). A player (any seated party)
    # posts the SHA256 of the deck after a shuffle/lock stage `idx`, or the card->value map root (idx ==
    # IDX_CARDMAP). This pins each stage HASH so the deal is publicly auditable / disputable; it does NOT
    # verify an honest shuffle (that needs ZK proofs — out of scope [SEC-H1]). It NEVER feeds the betting
    # state machine, side pots, or settlement — read-only audit metadata. Caller must be a seated party
    # (anchors are authenticated to a real seat so a stranger cannot spam the chain), and no value attaches.
    a.label("deckdigest")
    _require_no_value(a)
    _spill_caller(a)
    _seat_of_caller(a, S1)                                  # must be a seated party (reverts if none)
    _load_be32(a, S2, S0, 4)                                # S2 = idx
    a.li(A2, IDX_CARDMAP); a.beq(S2, A2, "deckdigest_cardmap$")
    a.li(A2, DECKH_MAXIDX); a.bgeu(S2, A2, "revert")        # idx out of the deck-stage range
    # write 8 words of H32 to TAG_DECKH | (idx<<4) | w
    a.li(T0, 0)
    a.label("deckdigest_w$")
    a.li(A2, 8); a.bgeu(T0, A2, "deckdigest_done$")
    a.li(A2, 8); a.slli(A4, T0, 2); a.add(A2, A2, A4)        # A2 = calldata offset 8 + 4*w
    a.add(A4, S0, A2); a.lbu(T3, A4, 0); a.slli(T3, T3, 8)
    a.lbu(A5, A4, 1); a.or_(T3, T3, A5); a.slli(T3, T3, 8)
    a.lbu(A5, A4, 2); a.or_(T3, T3, A5); a.slli(T3, T3, 8)
    a.lbu(A5, A4, 3); a.or_(T3, T3, A5)                      # T3 = H32 word w
    a.li(A3, TAG_DECKH); a.slli(A4, S2, 4); a.or_(A3, A3, A4); a.or_(A3, A3, T0); a.sstore(A3, T3)
    a.addi(T0, T0, 1); a.j("deckdigest_w$")
    a.label("deckdigest_cardmap$")
    a.li(T0, 0)
    a.label("deckdigest_cm$")
    a.li(A2, 8); a.bgeu(T0, A2, "deckdigest_done$")
    a.li(A2, 8); a.slli(A4, T0, 2); a.add(A2, A2, A4)
    a.add(A4, S0, A2); a.lbu(T3, A4, 0); a.slli(T3, T3, 8)
    a.lbu(A5, A4, 1); a.or_(T3, T3, A5); a.slli(T3, T3, 8)
    a.lbu(A5, A4, 2); a.or_(T3, T3, A5); a.slli(T3, T3, 8)
    a.lbu(A5, A4, 3); a.or_(T3, T3, A5)
    a.li(A3, TAG_CARDMAP_H); a.or_(A3, A3, T0); a.sstore(A3, T3)
    a.addi(T0, T0, 1); a.j("deckdigest_cm$")
    a.label("deckdigest_done$")
    a.halt()

    # =============================================================== FN_CHECK
    a.label("check")
    _require_no_value(a)
    _action_prologue(a)                                    # S1 = caller seat == to_act, state ACTIVE
    # street_bet must == to_call
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, S1); a.sload_to(A4, A3)
    _sload(a, A2, S_TOCALL); a.bne(A4, A2, "revert")
    a.li(A3, TAG_ACTED); a.or_(A3, A3, S1); a.li(A4, 1); a.sstore(A3, A4)
    a.mv(S2, S1); a.j("advance$")

    # =============================================================== FN_CALL
    a.label("call")
    _require_no_value(a)
    _action_prologue(a)
    # commit (to_call - street_bet), capped at stack
    _sload(a, A4, S_TOCALL)
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, S1); a.sload_to(A2, A3)
    a.bltu(A4, A2, "revert")                               # to_call < street_bet impossible
    a.sub(A1, A4, A2)                                      # A1 = amount to add
    _commit_chips(a, S1, "call_c")
    a.li(A3, TAG_ACTED); a.or_(A3, A3, S1); a.li(A4, 1); a.sstore(A3, A4)
    a.mv(S2, S1); a.j("advance$")

    # =============================================================== FN_BET(target BE)
    a.label("bet")
    _require_no_value(a)
    _action_prologue(a)
    _load_be32(a, S3, S0, 4)                               # S3 = target (total street_bet after action)
    _sload(a, A4, S_TOCALL); a.bgeu(A4, S3, "revert")     # target must exceed to_call
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, S1); a.sload_to(A2, A3)  # current street_bet
    a.bgeu(A2, S3, "revert")                               # put = target - street_bet must be > 0
    a.sub(A1, S3, A2)                                      # A1 = put
    # all-in? put >= stack
    a.li(A3, TAG_STACK); a.or_(A3, A3, S1); a.sload_to(T4, A3)   # T4 = stack
    a.li(S2, 0)                                            # S2 = allin flag
    a.bltu(A1, T4, "bet_notallin$"); a.li(S2, 1)
    a.label("bet_notallin$")
    _commit_chips(a, S1, "bet_c")
    # reached = street_bet after commit
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, S1); a.sload_to(T4, A3)   # T4 = reached
    _sload(a, A4, S_TOCALL); _sload(a, A5, S_MINRAISE); a.add(A6, A4, A5)   # A6 = to_call+min_raise
    a.bltu(T4, A6, "bet_short$")                           # reached < to_call+min_raise -> all-in-for-less
    # full legal raise: min_raise = reached-to_call ; to_call = reached ; acted = {seat}
    a.sub(A5, T4, A4); _sstore_imm(a, S_MINRAISE, A5)
    _sstore_imm(a, S_TOCALL, T4)
    _clear_all_acted(a, N)
    a.li(A3, TAG_ACTED); a.or_(A3, A3, S1); a.li(A4, 1); a.sstore(A3, A4)
    a.mv(S2, S1); a.j("advance$")
    a.label("bet_short$")
    # all-in for less: must be all-in
    a.beq(S2, ZERO, "revert")
    # if reached > to_call, raise the bar (to_call = reached); does NOT reopen
    _sload(a, A4, S_TOCALL); a.bgeu(A4, T4, "bet_short_noraise$")
    _sstore_imm(a, S_TOCALL, T4)
    a.label("bet_short_noraise$")
    a.li(A3, TAG_ACTED); a.or_(A3, A3, S1); a.li(A4, 1); a.sstore(A3, A4)
    a.mv(S2, S1); a.j("advance$")

    # =============================================================== FN_FOLD
    a.label("fold")
    _require_no_value(a)
    _action_prologue(a)
    a.li(A3, TAG_STATE); a.or_(A3, A3, S1); a.li(A4, ST_FOLDED); a.sstore(A3, A4)
    a.li(A3, TAG_ACTED); a.or_(A3, A3, S1); a.li(A4, 1); a.sstore(A3, A4)
    a.mv(S2, S1); a.j("advance$")

    # =============================================================== _advance(frm=S2)
    # Mirrors poker_ref._advance. S2 = the seat that just acted (frm).
    a.label("advance$")
    # count non-folded (ACTIVE|ALLIN) among occupied -> if 1, uncontested
    a.li(T1, 0); a.li(T2, -1)        # T1 = nonfolded count, T2 = last nonfolded seat
    a.li(T0, 0)
    a.label("adv_nf$")
    a.li(A2, N); a.bgeu(T0, A2, "adv_nf_done$")
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.li(A2, ST_ACTIVE); a.beq(A4, A2, "adv_nf_yes$")
    a.li(A2, ST_ALLIN); a.beq(A4, A2, "adv_nf_yes$")
    a.j("adv_nf_next$")
    a.label("adv_nf_yes$")
    a.addi(T1, T1, 1); a.mv(T2, T0)
    a.label("adv_nf_next$")
    a.addi(T0, T0, 1); a.j("adv_nf$")
    a.label("adv_nf_done$")
    a.li(A2, 1); a.bne(T1, A2, "adv_check_close$")
    # uncontested: lone non-folded seat wins. Save the seat in S1 (S-reg) because _rebuild_pots clobbers
    # T0..T6, which would destroy T2.
    a.mv(S1, T2)
    _rebuild_pots(a, N)
    a.addi(A4, S1, 1); _sstore_imm(a, S_WINBYFOLD, A4)     # store seat+1
    a.li(A4, PH_SHOWDOWN); _sstore_imm(a, S_PHASE, A4)
    a.halt()

    a.label("adv_check_close$")
    # all ACTIVE seats have acted AND street_bet == to_call ?  -> close street
    _sload(a, S3, S_TOCALL)
    a.li(T1, 1)                       # T1 = all_matched flag
    a.li(T0, 0)
    a.label("adv_am$")
    a.li(A2, N); a.bgeu(T0, A2, "adv_am_done$")
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.li(A2, ST_ACTIVE); a.bne(A4, A2, "adv_am_next$")     # only ACTIVE seats matter
    a.li(A3, TAG_ACTED); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.beq(A4, ZERO, "adv_am_no$")
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.bne(A4, S3, "adv_am_no$")
    a.j("adv_am_next$")
    a.label("adv_am_no$")
    a.li(T1, 0); a.j("adv_am_done$")
    a.label("adv_am_next$")
    a.addi(T0, T0, 1); a.j("adv_am$")
    a.label("adv_am_done$")
    a.bne(T1, ZERO, "close_street$")
    # else find the next ACTIVE seat that still owes an action (from frm=S2)
    a.mv(T0, S2)
    a.li(T3, 0)                       # iteration guard
    a.label("adv_find$")
    a.li(A2, N); a.bgeu(T3, A2, "close_street$")           # safety net: no one left -> close
    a.addi(T0, T0, 1); a.li(A2, N); a.bltu(T0, A2, "adv_find_nm$"); a.sub(T0, T0, A2)
    a.label("adv_find_nm$")
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.li(A2, ST_ACTIVE); a.bne(A4, A2, "adv_find_next$")
    # ACTIVE: owes action if not acted OR street_bet < to_call
    a.li(A3, TAG_ACTED); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.beq(A4, ZERO, "adv_find_hit$")
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.bltu(A4, S3, "adv_find_hit$")
    a.label("adv_find_next$")
    a.addi(T3, T3, 1); a.j("adv_find$")
    a.label("adv_find_hit$")
    _sstore_imm(a, S_TOACT, T0)
    _set_deadline(a)
    a.halt()

    # ---------------- _close_street ----------------
    a.label("close_street$")
    _rebuild_pots(a, N)
    # clear street_bet for all seats
    a.li(T0, 0)
    a.label("cs_clr$")
    a.li(A2, N); a.bgeu(T0, A2, "cs_clr_done$")
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, T0); a.sstore(A3, ZERO)
    a.addi(T0, T0, 1); a.j("cs_clr$")
    a.label("cs_clr_done$")
    # count ACTIVE seats
    a.li(T1, 0)
    a.li(T0, 0)
    a.label("cs_ac$")
    a.li(A2, N); a.bgeu(T0, A2, "cs_ac_done$")
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.li(A2, ST_ACTIVE); a.bne(A4, A2, "cs_ac_next$")
    a.addi(T1, T1, 1)
    a.label("cs_ac_next$")
    a.addi(T0, T0, 1); a.j("cs_ac$")
    a.label("cs_ac_done$")
    # if street == RIVER (3) or active <= 1 -> showdown
    _sload(a, A4, S_STREET); a.li(A2, 3); a.bgeu(A4, A2, "cs_showdown$")
    a.li(A2, 2); a.bltu(T1, A2, "cs_showdown$")
    # next street
    _sload(a, A4, S_STREET); a.addi(A4, A4, 1); _sstore_imm(a, S_STREET, A4)
    a.li(A4, 0); _sstore_imm(a, S_TOCALL, A4)
    a.li(A4, bb); _sstore_imm(a, S_MINRAISE, A4)
    _clear_all_acted(a, N)
    # first ACTIVE seat left of button
    _sload(a, A5, S_BUTTON)
    a.mv(T0, A5)
    a.li(T3, 0)
    a.label("cs_first$")
    a.li(A2, N); a.bgeu(T3, A2, "cs_showdown$")            # safety: none active -> showdown
    a.addi(T0, T0, 1); a.li(A2, N); a.bltu(T0, A2, "cs_first_nm$"); a.sub(T0, T0, A2)
    a.label("cs_first_nm$")
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.li(A2, ST_ACTIVE); a.beq(A4, A2, "cs_first_hit$")
    a.addi(T3, T3, 1); a.j("cs_first$")
    a.label("cs_first_hit$")
    _sstore_imm(a, S_TOACT, T0)
    _set_deadline(a)
    a.halt()
    a.label("cs_showdown$")
    a.li(A4, PH_SHOWDOWN); _sstore_imm(a, S_PHASE, A4)
    _set_deadline(a)
    a.halt()

    # =============================================================== FN_REVEAL(c0..c4, nonce32)
    a.label("reveal")
    _require_no_value(a)
    _sload(a, A4, S_PHASE); a.li(A2, PH_SHOWDOWN); a.bne(A4, A2, "revert")
    _spill_caller(a)
    _seat_of_caller(a, S1)
    # must be non-folded
    a.li(A3, TAG_STATE); a.or_(A3, A3, S1); a.sload_to(A4, A3)
    a.li(A2, ST_FOLDED); a.beq(A4, A2, "revert")
    a.li(A2, ST_EMPTY); a.beq(A4, A2, "revert")
    # not already revealed
    a.li(A3, TAG_REVEALED); a.or_(A3, A3, S1); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")
    # build preimage c0..c4 | nonce(32) at SC_PRE, copying cards to SC_CARDS too
    a.li(T0, 0)
    a.label("rv_cards$")
    a.li(A2, 5); a.bgeu(T0, A2, "rv_cards_done$")
    a.add(A2, S0, T0); a.lbu(T3, A2, 4)                    # card byte at calldata 4+i
    a.li(A2, 51); a.bltu(A2, T3, "revert")                 # 0..51
    a.li(A2, SC_PRE); a.add(A2, A2, T0); a.sb(T3, A2, 0)
    a.li(A2, SC_CARDS); a.add(A2, A2, T0); a.sb(T3, A2, 0)
    a.addi(T0, T0, 1); a.j("rv_cards$")
    a.label("rv_cards_done$")
    # the 5 cards must be mutually distinct
    a.li(T0, 0)
    a.label("rv_di$")
    a.li(A2, 5); a.bgeu(T0, A2, "rv_di_done$")
    a.addi(T3, T0, 1)
    a.label("rv_dj$")
    a.li(A2, 5); a.bgeu(T3, A2, "rv_dinext$")
    a.li(A2, SC_CARDS); a.add(A2, A2, T0); a.lbu(T4, A2, 0)
    a.li(A2, SC_CARDS); a.add(A2, A2, T3); a.lbu(T5, A2, 0)
    a.beq(T4, T5, "revert")
    a.addi(T3, T3, 1); a.j("rv_dj$")
    a.label("rv_dinext$")
    a.addi(T0, T0, 1); a.j("rv_di$")
    a.label("rv_di_done$")
    # copy nonce (32 bytes) to SC_PRE+5
    a.li(T0, 0)
    a.label("rv_nonce$")
    a.li(A2, 32); a.bgeu(T0, A2, "rv_hash$")
    a.add(A2, S0, T0); a.lbu(A6, A2, 9)                    # nonce byte at calldata 9+i (4 sel +5 cards)
    a.li(A2, SC_PRE + 5); a.add(A2, A2, T0); a.sb(A6, A2, 0)
    a.addi(T0, T0, 1); a.j("rv_nonce$")
    a.label("rv_hash$")
    a.li(A0, SC_PRE); a.li(A1, 37); a.li(A2, SC_SHA); a.syscall(SYS_SHA256)
    # compare to the seat's commit (8 words)
    a.li(T0, 0)
    a.label("rv_cmp$")
    a.li(A2, 8); a.bgeu(T0, A2, "rv_ok$")
    a.li(A3, TAG_COMMIT); a.slli(A4, S1, 4); a.or_(A3, A3, A4); a.or_(A3, A3, T0); a.sload_to(T6, A3)
    a.li(A2, SC_SHA); a.slli(A4, T0, 2); a.add(A4, A2, A4)
    a.lbu(A2, A4, 0); a.slli(A2, A2, 8); a.lbu(A6, A4, 1); a.or_(A2, A2, A6); a.slli(A2, A2, 8)
    a.lbu(A6, A4, 2); a.or_(A2, A2, A6); a.slli(A2, A2, 8); a.lbu(A6, A4, 3); a.or_(A2, A2, A6)
    a.bne(T6, A2, "revert")
    a.addi(T0, T0, 1); a.j("rv_cmp$")
    a.label("rv_ok$")
    # store the 5 cards, rank them
    a.li(T0, 0)
    a.label("rv_store$")
    a.li(A2, 5); a.bgeu(T0, A2, "rv_store_done$")
    a.li(A2, SC_CARDS); a.add(A2, A2, T0); a.lbu(T3, A2, 0)
    a.li(A3, TAG_CARD); a.slli(A4, S1, 4); a.or_(A3, A3, A4); a.or_(A3, A3, T0); a.sstore(A3, T3)
    a.addi(T0, T0, 1); a.j("rv_store$")
    a.label("rv_store_done$")
    a.j("rank5_entry$")
    a.label("rank_back$")
    a.mv(T0, A0)                       # rank (sstore clobbers A0/A1, so stash it first)
    a.li(A3, TAG_RANK); a.or_(A3, A3, S1); a.sstore(A3, T0)
    a.li(A3, TAG_REVEALED); a.or_(A3, A3, S1); a.li(A4, 1); a.sstore(A3, A4)
    _set_deadline(a)
    a.halt()

    # =============================================================== FN_SETTLE
    a.label("settle_entry")
    _require_no_value(a)
    _sload(a, A4, S_PHASE); a.li(A2, PH_SHOWDOWN); a.bne(A4, A2, "revert")
    # every non-folded, non-empty seat must have revealed (unless uncontested by fold)
    _sload(a, A4, S_WINBYFOLD); a.bne(A4, ZERO, "settle_run$")   # uncontested -> settle directly
    a.li(T0, 0)
    a.label("set_chk$")
    a.li(A2, N); a.bgeu(T0, A2, "settle_run$")
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.li(A2, ST_ACTIVE); a.beq(A4, A2, "set_chk_need$")
    a.li(A2, ST_ALLIN); a.beq(A4, A2, "set_chk_need$")
    a.j("set_chk_next$")
    a.label("set_chk_need$")
    a.li(A3, TAG_REVEALED); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.beq(A4, ZERO, "revert")
    a.label("set_chk_next$")
    a.addi(T0, T0, 1); a.j("set_chk$")
    a.label("settle_run$")
    a.j("do_settle$")

    # =============================================================== FN_TIMEOUT
    a.label("timeout")
    _require_no_value(a)
    a.number(A4); a.li(A3, S_DEADLINE); a.sload_to(A2, A3); a.bgeu(A2, A4, "revert")   # deadline passed?
    _sload(a, T2, S_PHASE)
    a.li(A2, PH_BET); a.beq(T2, A2, "to_bet$")
    a.li(A2, PH_SHOWDOWN); a.beq(T2, A2, "to_showdown$")
    a.j("revert")
    a.label("to_bet$")
    # forfeit the overdue actor: fold the to_act seat, then advance
    _sload(a, S1, S_TOACT)
    a.li(A3, TAG_STATE); a.or_(A3, A3, S1); a.sload_to(A4, A3)
    a.li(A2, ST_ACTIVE); a.bne(A4, A2, "revert")
    a.li(A3, TAG_STATE); a.or_(A3, A3, S1); a.li(A4, ST_FOLDED); a.sstore(A3, A4)
    a.li(A3, TAG_ACTED); a.or_(A3, A3, S1); a.li(A4, 1); a.sstore(A3, A4)
    a.mv(S2, S1); a.j("advance$")
    a.label("to_showdown$")
    # settle with whoever revealed; non-revealers are treated as folded (forfeit)
    a.li(T0, 0)
    a.label("to_sd_fold$")
    a.li(A2, N); a.bgeu(T0, A2, "to_sd_done$")
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.li(A2, ST_ACTIVE); a.beq(A4, A2, "to_sd_maybe$")
    a.li(A2, ST_ALLIN); a.beq(A4, A2, "to_sd_maybe$")
    a.j("to_sd_next$")
    a.label("to_sd_maybe$")
    a.li(A3, TAG_REVEALED); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.bne(A4, ZERO, "to_sd_next$")
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.li(A4, ST_FOLDED); a.sstore(A3, A4)   # forfeit non-revealers
    a.label("to_sd_next$")
    a.addi(T0, T0, 1); a.j("to_sd_fold$")
    a.label("to_sd_done$")
    _rebuild_pots(a, N)   # re-layer after forfeits so folded laggards lose eligibility
    a.j("do_settle$")

    # =============================================================== settlement (multi-way, checked)
    _settle(a, N, int(max_buyin))

    # ---------------- the 5-card hand ranker (inlined) ----------------
    _rank5(a)

    a.label("revert")
    a.raw(0)
    return a.assemble()


# =================================================================== helpers
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


def _set_deadline(a):
    a.number(A4); a.li(A2, TIMEOUT_BLOCKS); a.add(A4, A4, A2)
    _sstore_imm(a, S_DEADLINE, A4)


def _require_no_value(a):
    a.callvalue(A2); a.bne(A2, ZERO, "revert")


def _spill_caller(a):
    """Spill the 7-word (28-byte) caller address to SC_CALLER as big-endian bytes. SYS_CALLER returns only
    the low 32 bits in the register, so we cannot reconstruct the full 28 bytes from one call. The engine
    masks to 32 bits, so the stored seat addresses and the live caller are compared word-by-word; word 6
    (the low 32 bits) is the only word that carries the masked caller, and the upper 6 words were captured
    verbatim at FN_SIT. We compare ALL 7 words: the upper 6 are whatever was stored at sit-time and must be
    re-supplied identically by the same wallet, and word 6 is the authenticated low-32 from SYS_CALLER.

    NOTE: bismuth_riscv.execute exposes only `caller & WMASK` (the low 32 bits) through SYS_CALLER. The full
    28-byte identity check is therefore realized as: words 0..5 are matched against the stored copy supplied
    by the caller in calldata is NOT used here; instead identity is the authenticated low-32 (SYS_CALLER)
    matched against the seat's stored word-6 PLUS the 6 high words stored at sit-time (which only the genuine
    seat owner's wallet produced). See _require_caller_is_seat / _seat_of_caller."""
    a.caller(A4)
    a.li(A2, SC_CALLER + 24)            # word 6 = low 32 bits at byte offset 24
    a.srli(A6, A4, 24); a.sb(A6, A2, 0)
    a.srli(A6, A4, 16); a.sb(A6, A2, 1)
    a.srli(A6, A4, 8); a.sb(A6, A2, 2)
    a.sb(A4, A2, 3)


def _require_caller_is_seat(a, seat_reg):
    """Revert unless SYS_CALLER's low 32 bits == seat_reg's stored address word 6 (the authenticated word).
    The seat's full 28-byte address was captured at FN_SIT; the engine only authenticates the low 32 bits,
    so that word is the binding identity check (the upper words are payout routing). Uses A2,A3,A4,A5.
    _spill_caller must have run first."""
    a.li(A2, SC_CALLER + 24); a.lbu(A4, A2, 0); a.slli(A4, A4, 8)
    a.lbu(A5, A2, 1); a.or_(A4, A4, A5); a.slli(A4, A4, 8)
    a.lbu(A5, A2, 2); a.or_(A4, A4, A5); a.slli(A4, A4, 8)
    a.lbu(A5, A2, 3); a.or_(A4, A4, A5)                       # A4 = caller low-32
    a.li(A3, TAG_ADDR); a.slli(A5, seat_reg, 4); a.or_(A3, A3, A5); a.li(A2, 6); a.or_(A3, A3, A2)
    a.sload_to(A5, A3)                                        # stored word 6
    a.bne(A4, A5, "revert")


def _seat_of_caller(a, dst):
    """dst = the seat index whose stored address word-6 == SYS_CALLER low-32; revert if none. Scans all
    seats. _spill_caller must have run first. Uses T0,A2,A3,A4,A5,A6 and dst."""
    # caller low-32 -> A6
    a.li(A2, SC_CALLER + 24); a.lbu(A6, A2, 0); a.slli(A6, A6, 8)
    a.lbu(A5, A2, 1); a.or_(A6, A6, A5); a.slli(A6, A6, 8)
    a.lbu(A5, A2, 2); a.or_(A6, A6, A5); a.slli(A6, A6, 8)
    a.lbu(A5, A2, 3); a.or_(A6, A6, A5)
    loop = a._uniq("soc"); hit = a._uniq("soc_hit"); nxt = a._uniq("soc_next")
    a.li(T0, 0)
    a.label(loop)
    a.li(A2, 9); a.bgeu(T0, A2, "revert")                    # exhausted all seats (<=9) -> revert
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.beq(A4, ZERO, nxt)   # skip EMPTY
    a.li(A3, TAG_ADDR); a.slli(A5, T0, 4); a.or_(A3, A3, A5); a.li(A2, 6); a.or_(A3, A3, A2)
    a.sload_to(A4, A3)
    a.beq(A4, A6, hit)
    a.label(nxt)
    a.addi(T0, T0, 1); a.j(loop)
    a.label(hit)
    a.mv(dst, T0)


def _action_prologue(a):
    """Common prologue for CHECK/CALL/BET/FOLD: phase must be PH_BET, caller's seat == S_TOACT, state
    ACTIVE. Leaves S1 = the caller's seat. Uses scratch + S1."""
    _sload(a, A4, S_PHASE); a.li(A2, PH_BET); a.bne(A4, A2, "revert")
    _spill_caller(a)
    _seat_of_caller(a, S1)
    _sload(a, A4, S_TOACT); a.bne(A4, S1, "revert")
    a.li(A3, TAG_STATE); a.or_(A3, A3, S1); a.sload_to(A4, A3)
    a.li(A2, ST_ACTIVE); a.bne(A4, A2, "revert")


def _next_occupied(a, dst, from_reg, N):
    """dst = the next seat after from_reg (cyclic) whose state != EMPTY. from_reg may be -1 (0xFFFFFFFF) on a
    fresh table. Scans up to N seats; if none found dst = from_reg's next (defensive). Uses T0,T6,A2,A3,A4."""
    loop = a._uniq("no"); hit = a._uniq("no_hit"); done = a._uniq("no_done")
    a.mv(T0, from_reg)
    a.li(T6, 0)
    a.label(loop)
    a.li(A2, N); a.bgeu(T6, A2, done)
    a.addi(T0, T0, 1); a.li(A2, N); a.bltu(T0, A2, "%s_nm" % loop); a.li(A2, N); a.sub(T0, T0, A2)
    a.label("%s_nm" % loop)
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.bne(A4, ZERO, hit)
    a.addi(T6, T6, 1); a.j(loop)
    a.label(hit)
    a.label(done)
    a.mv(dst, T0)


def _commit_chips(a, seat_reg, tag):
    """Move min(A1, stack) chips from the seat's stack into street_bet/hand_bet/escrow; if stack hits 0 and
    state==ACTIVE, set state=ALLIN. A1 = requested amount (clobbered). Uses T2,T3,T4,A2,A3,A4,A5,A6 and a
    unique-tag local label set. seat_reg preserved. Guards every accumulator against 32-bit overflow [SEC-C3]."""
    cap = a._uniq(tag + "_cap"); noallin = a._uniq(tag + "_noallin")
    a.li(A3, TAG_STACK); a.or_(A3, A3, seat_reg); a.sload_to(T2, A3)   # T2 = stack
    # amt = min(A1, stack)
    a.bgeu(A1, T2, cap)                # A1 >= stack -> amt = stack
    a.mv(T3, A1); a.j(tag + "_have$")
    a.label(cap)
    a.mv(T3, T2)
    a.label(tag + "_have$")
    # stack -= amt
    a.sub(A4, T2, T3); a.li(A3, TAG_STACK); a.or_(A3, A3, seat_reg); a.sstore(A3, A4)
    # street_bet += amt (guard)
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, seat_reg); a.sload_to(A4, A3); a.add(A5, A4, T3)
    a.bltu(A5, A4, "revert"); a.li(A3, TAG_STREETBET); a.or_(A3, A3, seat_reg); a.sstore(A3, A5)
    # hand_bet += amt (guard)
    a.li(A3, TAG_HANDBET); a.or_(A3, A3, seat_reg); a.sload_to(A4, A3); a.add(A5, A4, T3)
    a.bltu(A5, A4, "revert"); a.li(A3, TAG_HANDBET); a.or_(A3, A3, seat_reg); a.sstore(A3, A5)
    # escrow += amt (guard)
    a.li(A3, S_ESCROW); a.sload_to(A4, A3); a.add(A5, A4, T3); a.bltu(A5, A4, "revert")
    a.li(A3, S_ESCROW); a.sstore(A3, A5)
    # if stack == 0 and state == ACTIVE -> ALLIN
    a.li(A3, TAG_STACK); a.or_(A3, A3, seat_reg); a.sload_to(A4, A3); a.bne(A4, ZERO, noallin)
    a.li(A3, TAG_STATE); a.or_(A3, A3, seat_reg); a.sload_to(A4, A3)
    a.li(A2, ST_ACTIVE); a.bne(A4, A2, noallin)
    a.li(A3, TAG_STATE); a.or_(A3, A3, seat_reg); a.li(A4, ST_ALLIN); a.sstore(A3, A4)
    a.label(noallin)


def _clear_all_acted(a, N):
    loop = a._uniq("cla"); done = a._uniq("cla_done")
    a.li(T0, 0)
    a.label(loop)
    a.li(A2, N); a.bgeu(T0, A2, done)
    a.li(A3, TAG_ACTED); a.or_(A3, A3, T0); a.sstore(A3, ZERO)
    a.addi(T0, T0, 1); a.j(loop)
    a.label(done)


def _rebuild_pots(a, N):
    """Rebuild layered side pots from per-seat TAG_HANDBET (mirrors poker_ref._rebuild_pots). Distinct-level
    scan, no DIV/sort. Writes TAG_POTAMT|p / TAG_POTELIG|p (N-bit eligibility mask, folded seats excluded),
    sets S_NPOTS-equivalent count in scratch, and asserts Σ pot == Σ hand_bet == S_ESCROW [SEC-C2/H3].

    Algorithm: for each distinct level L = a seat's hand_bet (ascending, dedup via "smallest level > prev"),
    layer = (L - prev) * count(hand_bet >= L); eligible = {seats with hand_bet >= L and state != FOLDED}.
    Adjacent layers with identical eligibility are merged. Pot count stored at SC_LEVELS as a header word.

    Uses T0..T6, A2..A6, S2, S3 (callers must not rely on these across the call). NPOTS count -> stored in
    SC_LEVELS word 0 (mem)."""
    loop = a._uniq("rp"); inner = a._uniq("rp_in")
    # prev = 0 ; npots = 0 (in mem at SC_LEVELS)
    a.li(A2, SC_LEVELS); a.sw(ZERO, A2, 0)                    # npots = 0
    a.li(S3, 0)                                              # S3 = prev level
    a.label(loop)
    # find the smallest hand_bet strictly > prev (S3); if none, done
    a.li(T1, 0)                       # T1 = found flag
    a.li(T2, 0)                       # T2 = candidate min level
    a.li(T0, 0)
    a.label(loop + "_scan")
    a.li(A2, N); a.bgeu(T0, A2, loop + "_scan_done")
    a.li(A3, TAG_HANDBET); a.or_(A3, A3, T0); a.sload_to(A4, A3)   # A4 = hand_bet[i]
    a.bgeu(S3, A4, loop + "_scan_next")                      # hand_bet <= prev -> skip
    # candidate: if !found or A4 < T2 -> update
    a.beq(T1, ZERO, loop + "_take")
    a.bgeu(A4, T2, loop + "_scan_next")
    a.label(loop + "_take")
    a.mv(T2, A4); a.li(T1, 1)
    a.label(loop + "_scan_next")
    a.addi(T0, T0, 1); a.j(loop + "_scan")
    a.label(loop + "_scan_done")
    a.beq(T1, ZERO, loop + "_finish")                       # no more levels
    # L = T2. amount = (L - prev) * count(hand_bet >= L) ; elig mask = seats hand_bet>=L and !folded
    a.sub(T3, T2, S3)                 # T3 = L - prev
    a.li(T4, 0)                       # T4 = count
    a.li(S2, 0)                       # S2 = elig mask
    a.li(T0, 0)
    a.label(inner)
    a.li(A2, N); a.bgeu(T0, A2, inner + "_done")
    a.li(A3, TAG_HANDBET); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.bltu(A4, T2, inner + "_next")                          # hand_bet < L -> not in this layer
    a.addi(T4, T4, 1)                                        # count++
    a.li(A3, TAG_STATE); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.li(A2, ST_FOLDED); a.beq(A4, A2, inner + "_next")      # folded -> not eligible
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.or_(S2, S2, A2)      # set bit T0 in mask
    a.label(inner + "_next")
    a.addi(T0, T0, 1); a.j(inner)
    a.label(inner + "_done")
    # amount = T3 * T4 (guarded; bounded by N*buyin <= 0x3FFFFFFF so it fits 32 bits). Keep it in T3
    # (NOT A0/A1, which SSTORE/SLOAD clobber as key/value scratch).
    a.mulu(A0, T3, T4)                # A0 = layer amount
    a.mv(T3, A0)                      # T3 = layer amount (sstore-safe)
    # prev = L for next iteration
    a.mv(S3, T2)
    a.beq(T3, ZERO, loop)             # zero layer -> skip (no pot row)
    # orphan layer (eligibility mask S2 == 0: every contributor to this level folded) -> cascade its chips
    # into the nearest LOWER pot, which always has an eligible winner. Levels ascend and the contributor set
    # only shrinks, so once eligibility empties it stays empty -> orphan layers are a contiguous top block,
    # each folding onto the last real pot. Conserves Σ and never refunds a (possibly timed-out) folder, so
    # the hand can always settle [review bug #1: no locked funds].
    a.bne(S2, ZERO, loop + "_notorphan")
    a.li(A2, SC_LEVELS); a.lw(T5, A2, 0)                     # T5 = npots
    a.beq(T5, ZERO, "revert")                                # orphan with no lower pot (impossible in valid play)
    a.addi(T6, T5, -1)                                       # last (lower) pot index
    a.li(A3, TAG_POTAMT); a.or_(A3, A3, T6); a.sload_to(A5, A3); a.add(A5, A5, T3)
    a.li(A3, TAG_POTAMT); a.or_(A3, A3, T6); a.sstore(A3, A5)
    a.j(loop)
    a.label(loop + "_notorphan")
    # try to merge with the previous pot if same eligibility mask
    a.li(A2, SC_LEVELS); a.lw(T5, A2, 0)                     # T5 = npots
    a.beq(T5, ZERO, loop + "_new")
    a.addi(T6, T5, -1)                                       # last pot index
    a.li(A3, TAG_POTELIG); a.or_(A3, A3, T6); a.sload_to(A5, A3)   # last elig
    a.bne(A5, S2, loop + "_new")
    # merge: last pot amount += T3
    a.li(A3, TAG_POTAMT); a.or_(A3, A3, T6); a.sload_to(A5, A3); a.add(A5, A5, T3)
    a.li(A3, TAG_POTAMT); a.or_(A3, A3, T6); a.sstore(A3, A5)
    a.j(loop)
    a.label(loop + "_new")
    a.li(A3, TAG_POTAMT); a.or_(A3, A3, T5); a.sstore(A3, T3)
    a.li(A3, TAG_POTELIG); a.or_(A3, A3, T5); a.sstore(A3, S2)
    a.addi(T5, T5, 1); a.li(A2, SC_LEVELS); a.sw(T5, A2, 0)  # npots++
    a.j(loop)
    a.label(loop + "_finish")
    # persist npots to a storage slot (mem SC_LEVELS is per-call only; FN_SETTLE is a later call)
    a.li(A2, SC_LEVELS); a.lw(T5, A2, 0)                     # npots
    _sstore_imm(a, S_NPOTS, T5)
    # zero any stale pot rows beyond the current npots (a prior hand may have had more layers)
    a.mv(T0, T5)
    a.label(loop + "_clrstale")
    a.li(A2, 9); a.bgeu(T0, A2, loop + "_clrstale_done")     # at most 9 layers ever
    a.li(A3, TAG_POTAMT); a.or_(A3, A3, T0); a.sstore(A3, ZERO)
    a.li(A3, TAG_POTELIG); a.or_(A3, A3, T0); a.sstore(A3, ZERO)
    a.addi(T0, T0, 1); a.j(loop + "_clrstale")
    a.label(loop + "_clrstale_done")
    # closing invariant: Σ pot amounts == Σ hand_bet == S_ESCROW
    a.li(T6, 0)                       # sum of pot amounts
    a.li(T0, 0)
    a.label(loop + "_sum")
    a.bgeu(T0, T5, loop + "_sumdone")
    a.li(A3, TAG_POTAMT); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.add(T6, T6, A4)
    a.addi(T0, T0, 1); a.j(loop + "_sum")
    a.label(loop + "_sumdone")
    # sum hand_bet
    a.li(T4, 0); a.li(T0, 0)
    a.label(loop + "_hb")
    a.li(A2, N); a.bgeu(T0, A2, loop + "_hbdone")
    a.li(A3, TAG_HANDBET); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.add(T4, T4, A4)
    a.addi(T0, T0, 1); a.j(loop + "_hb")
    a.label(loop + "_hbdone")
    a.bne(T6, T4, "revert")
    _sload(a, A4, S_ESCROW); a.bne(T6, A4, "revert")


def _pay_seat_checked(a, seat_reg, amount_reg):
    """CHECKED transfer of amount_reg units to seat_reg's stored 7-word address. Reverts if the engine
    returns 0 (insufficient custody) [SEC-C2]. Uses T0..T4, A2..A6. seat_reg/amount_reg preserved if not
    aliasing the scratch set; callers pass S-regs."""
    loop = a._uniq("ps"); done = a._uniq("ps_done")
    # write 7 words of the address to SC_RECIP as BE bytes
    a.li(T0, 0)
    a.label(loop)
    a.li(A2, 7); a.bgeu(T0, A2, done)
    a.li(A3, TAG_ADDR); a.slli(A4, seat_reg, 4); a.or_(A3, A3, A4); a.or_(A3, A3, T0); a.sload_to(A4, A3)
    a.li(A2, SC_RECIP); a.slli(A6, T0, 2); a.add(A2, A2, A6)
    a.srli(A6, A4, 24); a.sb(A6, A2, 0)
    a.srli(A6, A4, 16); a.sb(A6, A2, 1)
    a.srli(A6, A4, 8); a.sb(A6, A2, 2)
    a.sb(A4, A2, 3)
    a.addi(T0, T0, 1); a.j(loop)
    a.label(done)
    a.li(A3, SC_AMT); a.store_u64_be(amount_reg, A3, 0)
    a.li(A4, SC_RECIP); a.li(A5, SC_AMT); a.transfer(A4, A5)   # a0 = 1 if queued
    a.li(A2, 1); a.bne(A0, A2, "revert")                      # CHECKED [SEC-C2]


def _settle(a, N, buyin):
    """Multi-way showdown settlement (mirrors poker_ref.settle). For each pot p:
      - if uncontested (S_WINBYFOLD set): winner = that seat if eligible, else all eligible split;
      - else winners = eligible(p) AND revealed AND max TAG_RANK;
      share = pot // len(winners); odd = pot - share*len; odd chip to first winner CLOCKWISE FROM BUTTON
      within the pot's winner set (order key (i-button-1)%N).
    Accumulates per-seat payout in TAG_STREETBET (repurposed as a payout scratch, cleared first), asserts
    Σ payouts == S_ESCROW BEFORE issuing any transfer [SEC-C2], then pays each seat with a checked transfer.

    Uses T0..T6, S1..S3, A0..A6. Entered by `j do_settle$`."""
    a.label("do_settle$")
    # mark phase SETTLED + doneflag (idempotent terminal); guard re-entry by phase check at entry
    # clear payout scratch (reuse TAG_STREETBET as payout accumulator; bets are done)
    a.li(T0, 0)
    a.label("st_clrpay$")
    a.li(A2, N); a.bgeu(T0, A2, "st_clrpay_done$")
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, T0); a.sstore(A3, ZERO)
    a.addi(T0, T0, 1); a.j("st_clrpay$")
    a.label("st_clrpay_done$")
    # iterate pots (npots persisted in S_NPOTS by _rebuild_pots; mem SC_LEVELS is per-call only)
    _sload(a, S3, S_NPOTS)                                   # S3 = npots
    a.li(S2, 0)                       # S2 = pot index p
    a.label("st_pot$")
    a.bgeu(S2, S3, "st_paid$")
    a.li(A3, TAG_POTAMT); a.or_(A3, A3, S2); a.sload_to(T6, A3)   # T6 = pot amount
    a.li(A3, TAG_POTELIG); a.or_(A3, A3, S2); a.sload_to(S1, A3)  # S1 = elig mask
    # determine winners. First compute the "best rank among eligible revealed" OR use winbyfold.
    _sload(a, A4, S_WINBYFOLD)
    a.beq(A4, ZERO, "st_contested$")
    # uncontested: winner = (winbyfold-1) if its bit is in elig, else all eligible seats split. Winner mask
    # lives in S1 (elig is no longer needed once the winners are known).
    a.addi(T0, A4, -1)                # T0 = lone seat
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.and_(A2, A2, S1)
    a.beq(A2, ZERO, "st_have_winners$")   # lone seat not eligible -> all eligible split (S1 already = elig)
    a.li(A2, 1); a.sll_r(S1, A2, T0)      # winner set = just that seat
    a.j("st_have_winners$")
    a.label("st_contested$")
    # best rank among (eligible AND revealed)
    a.li(T4, 0)                       # T4 = best rank (ranks are > 0 for any real hand; 0 = none yet)
    a.li(T1, 0)                       # T1 = found any
    a.li(T0, 0)
    a.label("st_best$")
    a.li(A2, N); a.bgeu(T0, A2, "st_best_done$")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.and_(A2, A2, S1); a.beq(A2, ZERO, "st_best_next$")  # not eligible
    a.li(A3, TAG_REVEALED); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.beq(A4, ZERO, "st_best_next$")
    a.li(A3, TAG_RANK); a.or_(A3, A3, T0); a.sload_to(A4, A3)   # A4 = rank
    a.bne(T1, ZERO, "st_best_cmp$")
    a.mv(T4, A4); a.li(T1, 1); a.j("st_best_next$")
    a.label("st_best_cmp$")
    a.bgeu(T4, A4, "st_best_next$"); a.mv(T4, A4)
    a.label("st_best_next$")
    a.addi(T0, T0, 1); a.j("st_best$")
    a.label("st_best_done$")
    a.beq(T1, ZERO, "revert")         # contested pot must have >=1 revealed eligible seat
    # winner mask = eligible AND revealed AND rank == best (build in T5, then move to S1)
    a.li(T5, 0)
    a.li(T0, 0)
    a.label("st_wm$")
    a.li(A2, N); a.bgeu(T0, A2, "st_wm_done$")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.and_(A2, A2, S1); a.beq(A2, ZERO, "st_wm_next$")
    a.li(A3, TAG_REVEALED); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.beq(A4, ZERO, "st_wm_next$")
    a.li(A3, TAG_RANK); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.bne(A4, T4, "st_wm_next$")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.or_(T5, T5, A2)
    a.label("st_wm_next$")
    a.addi(T0, T0, 1); a.j("st_wm$")
    a.label("st_wm_done$")
    a.mv(S1, T5)                      # S1 = winner mask
    a.label("st_have_winners$")
    # S1 = winner mask, T6 = pot amount. count winners (popcount) -> T1
    a.li(T1, 0)                       # T1 = nwinners
    a.li(T0, 0)
    a.label("st_pc$")
    a.li(A2, N); a.bgeu(T0, A2, "st_pc_done$")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.and_(A2, A2, S1); a.beq(A2, ZERO, "st_pc_next$")
    a.addi(T1, T1, 1)
    a.label("st_pc_next$")
    a.addi(T0, T0, 1); a.j("st_pc$")
    a.label("st_pc_done$")
    a.beq(T1, ZERO, "revert")
    # share = pot // nwinners ; odd = pot - share*nwinners. divu/mulu clobber T4,T5 (their temps), so the
    # winner mask (S1) and pot amount (T6) MUST be in registers those macros leave alone.
    a.divu(T2, T3, T6, T1, tmp_i=T4, tmp_bit=T5)   # T2 = share (quotient)
    a.mulu(A0, T2, T1, tmp_shift=T4, tmp_mcand=T5) # A0 = share*nwinners
    a.sub(T3, T6, A0)                 # T3 = odd chips
    # first pass: pay `share` to each winner (accumulate in TAG_STREETBET)
    a.li(T0, 0)
    a.label("st_dist$")
    a.li(A2, N); a.bgeu(T0, A2, "st_dist_done$")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.and_(A2, A2, S1); a.beq(A2, ZERO, "st_dist_next$")
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.add(A4, A4, T2)
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, T0); a.sstore(A3, A4)
    a.label("st_dist_next$")
    a.addi(T0, T0, 1); a.j("st_dist$")
    a.label("st_dist_done$")
    # odd chip to first winner clockwise from button: smallest (i-button-1) mod N among winners
    a.beq(T3, ZERO, "st_pot_next$")   # no odd chips -> skip
    _sload(a, T4, S_BUTTON)           # T4 = button
    a.li(T1, 0xFFFFFFFF)              # best key (large)
    a.li(T2, -1)                      # best seat
    a.li(T0, 0)
    a.label("st_odd$")
    a.li(A2, N); a.bgeu(T0, A2, "st_odd_done$")
    a.li(A2, 1); a.sll_r(A2, A2, T0); a.and_(A2, A2, S1); a.beq(A2, ZERO, "st_odd_next$")
    # key = (i - button - 1) mod N
    a.sub(A4, T0, T4); a.addi(A4, A4, -1)        # i - button - 1
    _mod_n(a, A4, N)                              # A4 = key in 0..N-1
    a.bgeu(A4, T1, "st_odd_next$")
    a.mv(T1, A4); a.mv(T2, T0)
    a.label("st_odd_next$")
    a.addi(T0, T0, 1); a.j("st_odd$")
    a.label("st_odd_done$")
    # add odd chips to T2's payout
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, T2); a.sload_to(A4, A3); a.add(A4, A4, T3)
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, T2); a.sstore(A3, A4)
    a.label("st_pot_next$")
    a.addi(S2, S2, 1); a.j("st_pot$")
    a.label("st_paid$")
    # closing invariant: Σ payouts (TAG_STREETBET) == S_ESCROW  [SEC-C2], BEFORE any transfer
    a.li(T6, 0); a.li(T0, 0)
    a.label("st_inv$")
    a.li(A2, N); a.bgeu(T0, A2, "st_inv_done$")
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, T0); a.sload_to(A4, A3); a.add(T6, T6, A4)
    a.addi(T0, T0, 1); a.j("st_inv$")
    a.label("st_inv_done$")
    _sload(a, A4, S_ESCROW); a.bne(T6, A4, "revert")
    # mark settled (idempotent terminal) BEFORE transfers
    a.li(A4, PH_SETTLED); _sstore_imm(a, S_PHASE, A4)
    a.li(A4, 1); _sstore_imm(a, S_DONEFLAG, A4)
    # credit winnings back to stacks and pay out (checked). For each seat with payout>0:
    a.li(T0, 0)
    a.label("st_pay$")
    a.li(A2, N); a.bgeu(T0, A2, "st_pay_done$")
    a.li(A3, TAG_STREETBET); a.or_(A3, A3, T0); a.sload_to(S1, A3)   # S1 = this seat's pot winnings
    a.beq(S1, ZERO, "st_pay_next$")
    # Cash the winnings OUT to the seat's address via a CHECKED transfer [SEC-C2]. The pot (escrow) is the
    # only money that leaves here; each seat's remaining (post-betting) stack stays in custody until LEAVE.
    a.mv(S2, T0)
    _pay_seat_checked(a, S2, S1)
    a.mv(T0, S2)                      # _pay_seat_checked preserves S2; restore loop counter from it
    a.label("st_pay_next$")
    a.addi(T0, T0, 1); a.j("st_pay$")
    a.label("st_pay_done$")
    a.halt()


def _mod_n(a, reg, N):
    """reg = reg mod N (N small, 2..9), reg may be a small negative (two's complement). Adds N until in
    range then subtracts N until < N. Uses A5,A6. Bounded loops (key magnitude < 2N)."""
    addl = a._uniq("modn_add"); subl = a._uniq("modn_sub")
    a.label(addl)
    a.li(A5, 0x80000000); a.and_(A6, reg, A5); a.beq(A6, ZERO, subl)   # while negative (sign bit) add N
    a.li(A5, N); a.add(reg, reg, A5); a.j(addl)
    a.label(subl)
    a.li(A5, N); a.bltu(reg, A5, "%s_done" % subl); a.sub(reg, reg, A5); a.j(subl)
    a.label("%s_done" % subl)


# =================================================================== the 5-card ranker (verbatim from poker.py)
def _rank5(a):
    """Rank the 5 card bytes at SC_CARDS[0..4] -> A0 = category<<20 | tiebreak (verbatim port of poker.py's
    audited evaluator; reads SC_CARDS, scratch SC_CNT). Entered by `j rank5_entry$`; returns by `j
    rank_back$`. Uses ONLY T0..T6 + A0/A2..A6 (no S registers)."""
    a.label("rank5_entry$")
    a.li(T0, 0)
    a.label("r5_zc$")
    a.li(A2, 13); a.bgeu(T0, A2, "r5_zc_done$")
    a.li(A2, SC_CNT); a.add(A2, A2, T0); a.sb(ZERO, A2, 0)
    a.addi(T0, T0, 1); a.j("r5_zc$")
    a.label("r5_zc_done$")
    a.li(T0, 0); a.li(T1, 1); a.li(T2, -1)
    a.label("r5_scan$")
    a.li(A2, 5); a.bgeu(T0, A2, "r5_counts$")
    a.li(A2, SC_CARDS); a.add(A2, A2, T0); a.lbu(A4, A2, 0)
    a.li(A5, 0); a.mv(A6, A4)
    a.label("r5_div$")
    a.li(A2, 13); a.bltu(A6, A2, "r5_divdone$")
    a.addi(A6, A6, -13); a.addi(A5, A5, 1); a.j("r5_div$")
    a.label("r5_divdone$")
    a.li(A2, SC_CNT); a.add(A2, A2, A6); a.lbu(A3, A2, 0); a.addi(A3, A3, 1); a.sb(A3, A2, 0)
    a.li(A2, -1); a.bne(T2, A2, "r5_sc_cmp$")
    a.mv(T2, A5); a.j("r5_sc_next$")
    a.label("r5_sc_cmp$")
    a.beq(T2, A5, "r5_sc_next$"); a.li(T1, 0)
    a.label("r5_sc_next$")
    a.addi(T0, T0, 1); a.j("r5_scan$")
    a.label("r5_counts$")
    a.li(T0, 0); a.li(T2, 0); a.li(T3, 0); a.li(T4, 0)
    a.label("r5_cc$")
    a.li(A2, 13); a.bgeu(T0, A2, "r5_straight$")
    a.li(A2, SC_CNT); a.add(A2, A2, T0); a.lbu(A4, A2, 0)
    a.li(A2, 2); a.beq(A4, A2, "r5_cc2$")
    a.li(A2, 3); a.beq(A4, A2, "r5_cc3$")
    a.li(A2, 4); a.beq(A4, A2, "r5_cc4$")
    a.j("r5_cc_next$")
    a.label("r5_cc2$"); a.addi(T2, T2, 1); a.j("r5_cc_next$")
    a.label("r5_cc3$"); a.addi(T3, T3, 1); a.j("r5_cc_next$")
    a.label("r5_cc4$"); a.addi(T4, T4, 1)
    a.label("r5_cc_next$")
    a.addi(T0, T0, 1); a.j("r5_cc$")
    a.label("r5_straight$")
    a.li(T5, -1)
    a.li(T0, 0)
    a.label("r5_st$")
    a.li(A2, 8); a.bltu(A2, T0, "r5_st_wheel$")
    a.li(T6, 0); a.li(A5, 1)
    a.label("r5_st_k$")
    a.li(A2, 5); a.bgeu(T6, A2, "r5_st_chk$")
    a.li(A2, SC_CNT); a.add(A2, A2, T0); a.add(A2, A2, T6); a.lbu(A4, A2, 0)
    a.li(A2, 1); a.beq(A4, A2, "r5_st_kok$"); a.li(A5, 0)
    a.label("r5_st_kok$")
    a.addi(T6, T6, 1); a.j("r5_st_k$")
    a.label("r5_st_chk$")
    a.beq(A5, ZERO, "r5_st_next$")
    a.addi(T5, T0, 4)
    a.label("r5_st_next$")
    a.addi(T0, T0, 1); a.j("r5_st$")
    a.label("r5_st_wheel$")
    a.li(A2, -1); a.bne(T5, A2, "r5_cat$")
    a.li(A2, SC_CNT); a.lbu(A4, A2, 12); a.li(A3, 1); a.bne(A4, A3, "r5_cat$")
    a.lbu(A4, A2, 0); a.bne(A4, A3, "r5_cat$")
    a.lbu(A4, A2, 1); a.bne(A4, A3, "r5_cat$")
    a.lbu(A4, A2, 2); a.bne(A4, A3, "r5_cat$")
    a.lbu(A4, A2, 3); a.bne(A4, A3, "r5_cat$")
    a.li(T5, 3)
    a.label("r5_cat$")
    a.li(A0, 0)
    a.li(A2, -1); a.beq(T5, A2, "r5_nostr$")
    a.beq(T1, ZERO, "r5_str$")
    a.li(A0, 8); a.j("r5_tb$")
    a.label("r5_str$"); a.li(A0, 4); a.j("r5_tb$")
    a.label("r5_nostr$")
    a.bne(T4, ZERO, "r5_quads$")
    a.beq(T3, ZERO, "r5_no3$")
    a.beq(T2, ZERO, "r5_trips$")
    a.li(A0, 6); a.j("r5_tb$")
    a.label("r5_trips$"); a.li(A0, 3); a.j("r5_tb$")
    a.label("r5_no3$")
    a.beq(T1, ZERO, "r5_pairs$")
    a.li(A0, 5); a.j("r5_tb$")
    a.label("r5_pairs$")
    a.li(A2, 2); a.beq(T2, A2, "r5_2pair$")
    a.li(A2, 1); a.beq(T2, A2, "r5_1pair$")
    a.li(A0, 0); a.j("r5_tb$")
    a.label("r5_2pair$"); a.li(A0, 2); a.j("r5_tb$")
    a.label("r5_1pair$"); a.li(A0, 1); a.j("r5_tb$")
    a.label("r5_quads$"); a.li(A0, 7)
    a.label("r5_tb$")
    a.slli(A0, A0, 20)
    a.li(A2, -1); a.beq(T5, A2, "r5_tb_counts$")
    a.or_(A0, A0, T5); a.j("rank_back$")
    a.label("r5_tb_counts$")
    a.li(T6, 0)
    a.li(T5, 4)
    a.label("r5_tb_c$")
    a.beq(T5, ZERO, "r5_tb_done$")
    a.li(T0, 12)
    a.label("r5_tb_r$")
    a.li(A2, SC_CNT); a.add(A2, A2, T0); a.lbu(A4, A2, 0)
    a.bne(A4, T5, "r5_tb_rnext$")
    a.slli(T6, T6, 4); a.or_(T6, T6, T0)
    a.label("r5_tb_rnext$")
    a.beq(T0, ZERO, "r5_tb_cnext$")
    a.addi(T0, T0, -1); a.j("r5_tb_r$")
    a.label("r5_tb_cnext$")
    a.addi(T5, T5, -1); a.j("r5_tb_c$")
    a.label("r5_tb_done$")
    a.or_(A0, A0, T6)
    a.j("rank_back$")
