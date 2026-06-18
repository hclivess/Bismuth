"""
poker.py — heads-up (2-player) Texas Hold'em as one hand-authored RV32I contract for the Bismuth in-core
VM. The chain is the TRUSTLESS REFEREE: it escrows the pot, anchors the card commitments, enforces play,
and settles the showdown ON-CHAIN (verifying revealed hole cards against their commitments and evaluating
the best five-card hand) — no operator, no custodian.

THE HARD PART (mental poker) lives OFF-CHAIN, by necessity
  Real poker needs PRIVATE hole cards with no trusted dealer. The VM has no randomness and cannot do the
  modular-exponentiation crypto cheaply, and secret keys can never be on-chain — so the deal itself (a
  cooperative commutative-cipher shuffle: each player encrypts the deck with a secret key, cards are dealt
  by selective decryption so each player sees only their own) runs in the two players' clients (web/poker/).
  This contract anchors that deal: each player posts a SHA256 commitment to its two hole cards BEFORE acting,
  so the hand cannot be changed later; the showdown verifies the reveal against the commit and that the five
  cards a player plays are distinct (no card reused within a hand) and in range, then evaluates the hand.
  What the chain guarantees on its own: stakes are escrowed, neither player can spend the other's stake, a
  folder/timed-out player forfeits, hole cards match their commitments, and the better five-card hand wins
  the pot. (Global 9-card uniqueness across both hands + board rests on the off-chain deal — see LIMITS.)

FORMAT (this demo): heads-up ALL-IN Hold'em. Each player stakes BUYIN; then P0 (button) PLAYs or FOLDs, P1
  CALLs or FOLDs. If both are in, BOTH players post the agreed 5-card board and both reveal for a 2*BUYIN
  showdown (split on tie). Chip-by-chip street betting (raises, side pots) is the documented extension; the
  trustless core — escrow + commit-reveal + on-chain hand evaluation + settlement — is identical.

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>"); first 4 bytes = selector:
  FN_STAKE0 (0)  — sel | addr(28); ATTACH BUYIN. P0 stakes + records its payout address (once, post-deploy).
  FN_JOIN   (1)  — sel | addr(28); ATTACH BUYIN. P1 stakes + records its payout address.
  FN_COMMIT (2)  — sel | commit(32). Caller posts SHA256(hole0|hole1|nonce). Both -> P0 to act.
  FN_PLAY   (3)  — P0 stays in (button), or P1 calls. P0 PLAY -> P1 to act; P1 CALL -> board phase.
  FN_FOLD   (4)  — the actor folds; the opponent wins the whole pot immediately.
  FN_BOARD  (5)  — sel | c0..c4 (5 bytes, each 0..51, mutually distinct). BOTH players must post the SAME 5
                   community cards (the board is not committed, so neither side can choose it alone). The
                   second matching post -> showdown; a disagreement is resolved by FN_TIMEOUT (void + refund).
  FN_REVEAL (6)  — sel | hole0(1) hole1(1) nonce(32) | idx0..idx4(5 bytes, each 0..6 into [hole0,hole1,
                   board0..4]). Verifies the commitment + 5 distinct in-range indices whose resolved cards are
                   pairwise distinct, ranks the chosen five; once both reveal, pays the better hand (split tie).
  FN_TIMEOUT(7)  — permissionless once the phase deadline (SYS_NUMBER) has passed, so escrow can never wedge:
                   WAIT_P1 -> refund P0 (nobody joined); COMMIT -> the lone committer wins, or split-refund if
                   neither committed; ACT_P0/ACT_P1 -> the overdue actor forfeits; BOARD -> void + refund both
                   (no board agreement); SHOWDOWN -> the sole revealer wins, or split-refund if neither reveals.
  Identity is the AUTHENTICATED caller captured at stake/join (not the calldata payout address), so no one can
  occupy both seats or drive another player's turns. Attach BIS ONLY to FN_STAKE0 / FN_JOIN — every other
  selector reverts if value is attached (the engine then refunds it). A revert commits nothing, transfers
  nothing.

HONEST LIMITS (demo-grade; mirror the other VM demos): off-chain deal (secrecy/shuffle fairness rest on the
clients' mental-poker protocol; the chain checks commitments + card-distinctness, not the shuffle); all-in
only (no chip-by-chip betting); heads-up only; 32-bit unit amounts (2*BUYIN <= 2^32-1). Because the board is
agreed (not deal-committed), a player who would lose can refuse to co-sign it / reveal and force a
deadline VOID that refunds both stakes — i.e. a sore loser can dodge the loss for a refund, but can never
STEAL the pot or lock the opponent's stake (every stalled phase has a deadline exit).
"""
import hashlib

from asmtools import (Asm, A0, A1, A2, A3, A4, A5, A6, S0, S1, S2, S3, T0, T1, T2, T3, T4, T5, T6, ZERO,
                      SYS_SHA256)

# selectors
FN_STAKE0 = 0
FN_JOIN = 1
FN_COMMIT = 2
FN_PLAY = 3
FN_FOLD = 4
FN_BOARD = 5
FN_REVEAL = 6
FN_TIMEOUT = 7

# phases
PH_OPEN = 0          # deployed; P0 not yet staked
PH_WAIT_P1 = 1       # P0 staked; waiting for P1
PH_COMMIT = 2        # both staked; waiting for both hole-card commitments
PH_ACT_P0 = 3        # both committed; P0 to act (PLAY/FOLD)
PH_ACT_P1 = 4        # P0 in; P1 to act (CALL via FN_PLAY / FOLD)
PH_BOARD = 5         # both in; waiting for the 5-card board
PH_SHOWDOWN = 6      # board posted; waiting for reveals
PH_DONE = 7

# fixed storage slots
S_PHASE = 1
S_DEADLINE = 2       # block height after which the to-act/reveal player can be timed out
S_REVEAL0 = 3        # P0 revealed (0/1)
S_REVEAL1 = 4        # P1 revealed (0/1)
S_RANK0 = 5
S_RANK1 = 6
S_DONEFLAG = 7
S_COMMITTED0 = 8     # P0 has committed (0/1) — a DEDICATED flag, never the commit word0 (which can be 0)
S_COMMITTED1 = 9     # P1 has committed (0/1)
S_BOARDER = 10       # who proposed the board: 0 none, 1 P0, 2 P1 (both must post matching cards to advance)

# domains (| word/index)
TAG_P0ADDR = 0x10000000   # w in 0..6 : P0 payout address word
TAG_P1ADDR = 0x11000000
TAG_COMMIT0 = 0x12000000  # w in 0..7 : P0 hole-card commit (32 bytes)
TAG_COMMIT1 = 0x13000000
TAG_BOARD = 0x14000000    # i in 0..4 : board card (0..51)
TAG_HOLE0 = 0x15000000    # j in 0,1  : P0 revealed hole card
TAG_HOLE1 = 0x16000000
TAG_P0ID = 0x17000000     # P0 identity = low-32 of the staking CALLER (authenticated, not the payout addr)
TAG_P1ID = 0x18000000     # P1 identity = low-32 of the joining CALLER

TIMEOUT_BLOCKS = 60

# mem scratch (well past code + calldata)
SCRATCH = 24000
SC_RECIP = SCRATCH         # 28-byte transfer recipient
SC_AMT = SCRATCH + 32      # 8-byte BE transfer amount
SC_PRE = SCRATCH + 64      # 34-byte SHA256 preimage: hole0|hole1|nonce
SC_SHA = SCRATCH + 128     # 32-byte SHA256 output
SC_CARDS = SCRATCH + 192   # 5 chosen card bytes
SC_RANKS = SCRATCH + 200   # 5 chosen-index bytes (for the distinctness scan in _gather5)
SC_CNT = SCRATCH + 216     # 13-byte rank-count array used by the hand evaluator
SC_PROP = SCRATCH + 232    # 5 proposed board-card bytes (FN_BOARD agreement check)


def party_id_of(address):
    try:
        return int(str(address), 16) & 0xFFFFFFFF
    except Exception:
        return int.from_bytes(hashlib.blake2b(str(address).encode(), digest_size=32).digest(),
                              "big") & 0xFFFFFFFF


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
    """Revert if BIS is attached to a selector that must not consume callvalue; vm_engine then refunds the
    sender on the revert, so attached value can never be silently stranded in the contract's custody. Uses A2.
    (Mirrors amm.py's _require_no_value. SYS_CALLVALUE clobbers only a0, never the live T1/S0.)"""
    a.callvalue(A2); a.bne(A2, ZERO, "revert")


def _which_player(a, dst):
    """dst = 0 if caller(T1)==P0 identity, 1 if ==P1 identity, else revert. Identity is the AUTHENTICATED
    caller captured at stake/join (TAG_P0ID/TAG_P1ID), NOT the calldata-supplied payout address. Uses A3,A4
    and unique local labels."""
    p1 = a._uniq("wp_p1"); done = a._uniq("wp_done")
    a.li(A3, TAG_P0ID); a.sload_to(A4, A3); a.li(dst, 0); a.beq(A4, T1, done)
    a.li(A3, TAG_P1ID); a.sload_to(A4, A3); a.li(dst, 1); a.beq(A4, T1, done)
    a.j("revert")
    a.label(done)


def _other(a, dst, p_reg):
    a.li(A2, 1); a.sub(dst, A2, p_reg)


def _addr_tag_of(a, dst, p_reg):
    """dst = TAG_P0ADDR if p==0 else TAG_P1ADDR. Uses A2."""
    skip = a._uniq("atag")
    a.li(dst, TAG_P0ADDR); a.beq(p_reg, ZERO, skip)
    a.li(dst, TAG_P1ADDR)
    a.label(skip)


def _addr_to_scratch(a, tag_reg):
    """Write the 7 address words at base tag_reg as 28 big-endian bytes at SC_RECIP. Uses T0,A2,A3,A4,A6."""
    loop = a._uniq("a2s"); done = a._uniq("a2s_done")
    a.li(T0, 0)
    a.label(loop)
    a.li(A2, 7); a.bgeu(T0, A2, done)
    a.mv(A3, tag_reg); a.add(A3, A3, T0); a.sload_to(A4, A3)
    a.li(A2, SC_RECIP); a.slli(A6, T0, 2); a.add(A2, A2, A6)
    a.srli(A6, A4, 24); a.sb(A6, A2, 0)
    a.srli(A6, A4, 16); a.sb(A6, A2, 1)
    a.srli(A6, A4, 8); a.sb(A6, A2, 2)
    a.sb(A4, A2, 3)
    a.addi(T0, T0, 1); a.j(loop)
    a.label(done)


def _pay_pot(a, p_reg, amount):
    """Pay `amount` units to player p_reg's stored address. Uses T3 + scratch. p_reg in {0,1}."""
    _addr_tag_of(a, A5, p_reg)
    _addr_to_scratch(a, A5)
    a.li(T3, int(amount))
    a.li(A3, SC_AMT); a.store_u64_be(T3, A3, 0)
    a.li(A4, SC_RECIP); a.transfer(A4, A3)


def build(p0_id, buyin):
    """Assemble the heads-up all-in Hold'em contract. p0_id = party_id_of(P0); buyin = each player's stake
    (units). buyin and 2*buyin must fit a 32-bit word."""
    if not (0 <= int(p0_id) <= 0xFFFFFFFF):
        raise ValueError("p0_id must fit a 32-bit word")
    if not (0 < int(buyin)) or int(buyin) * 2 > 0xFFFFFFFF:
        raise ValueError("need buyin > 0 and 2*buyin <= 2^32-1")
    a = Asm()
    POT = int(buyin) * 2

    # dispatch: S0 = calldata ptr, T0 = selector, T1 = caller fold
    a.mv(S0, A0)
    _load_be32(a, T0, S0, 0)
    a.caller(T1)
    a.li(A2, FN_STAKE0); a.beq(T0, A2, "stake0")
    a.li(A2, FN_JOIN);   a.beq(T0, A2, "join")
    a.li(A2, FN_COMMIT); a.beq(T0, A2, "commit")
    a.li(A2, FN_PLAY);   a.beq(T0, A2, "play")
    a.li(A2, FN_FOLD);   a.beq(T0, A2, "fold")
    a.li(A2, FN_BOARD);  a.beq(T0, A2, "board")
    a.li(A2, FN_REVEAL); a.beq(T0, A2, "reveal")
    a.li(A2, FN_TIMEOUT);a.beq(T0, A2, "timeout")
    a.j("revert")

    # ---- FN_STAKE0(addr): P0 stakes BUYIN (OPEN -> WAIT_P1) -----------------------------------------
    a.label("stake0")
    _sload(a, A4, S_PHASE); a.bne(A4, ZERO, "revert")
    a.li(A2, int(p0_id)); a.bne(T1, A2, "revert")
    a.callvalue(A4); a.li(A2, int(buyin)); a.bne(A4, A2, "revert")
    for w in range(7):
        _load_be32(a, T3, S0, 4 + w * 4); a.li(A3, TAG_P0ADDR | w); a.sstore(A3, T3)
    a.li(A3, TAG_P0ID); a.sstore(A3, T1)                   # bind P0 identity to the authenticated caller
    a.li(A4, PH_WAIT_P1); _sstore_imm(a, S_PHASE, A4); _set_deadline(a)   # arm WAIT_P1 timeout/refund
    a.halt()

    # ---- FN_JOIN(addr): P1 stakes BUYIN (WAIT_P1 -> COMMIT) -----------------------------------------
    a.label("join")
    _sload(a, A4, S_PHASE); a.li(A2, PH_WAIT_P1); a.bne(A4, A2, "revert")
    a.li(A2, int(p0_id)); a.beq(T1, A2, "revert")          # joiner (caller) must not be P0 (no self-join)
    a.callvalue(A4); a.li(A2, int(buyin)); a.bne(A4, A2, "revert")
    for w in range(7):
        _load_be32(a, T3, S0, 4 + w * 4); a.li(A3, TAG_P1ADDR | w); a.sstore(A3, T3)
    a.li(A3, TAG_P1ID); a.sstore(A3, T1)                   # bind P1 identity to the authenticated caller
    a.li(A4, PH_COMMIT); _sstore_imm(a, S_PHASE, A4); _set_deadline(a)    # arm PH_COMMIT timeout
    a.halt()

    # ---- FN_COMMIT(commit32): store caller's commit; both committed -> P0 to act --------------------
    a.label("commit")
    _require_no_value(a)
    _sload(a, A4, S_PHASE); a.li(A2, PH_COMMIT); a.bne(A4, A2, "revert")
    _which_player(a, S1)
    # commit tag base -> S2 ; my dedicated committed-flag slot -> S3 (a hash word can legitimately be 0, so
    # the flag — never a commit word — is the sole presence sentinel for freshness AND the advance gate)
    cm1 = a._uniq("cm_p1")
    a.li(S2, TAG_COMMIT0); a.li(S3, S_COMMITTED0); a.beq(S1, ZERO, cm1)
    a.li(S2, TAG_COMMIT1); a.li(S3, S_COMMITTED1)
    a.label(cm1)
    a.mv(A3, S3); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")   # fresh commit only (flag == 0)
    for w in range(8):
        _load_be32(a, T3, S0, 4 + w * 4)
        a.mv(A3, S2); a.li(A2, w); a.or_(A3, A3, A2); a.sstore(A3, T3)
    a.li(A4, 1); a.mv(A3, S3); a.sstore(A3, A4)              # set my committed flag
    cmhalt = a._uniq("cm_halt")
    a.li(A3, S_COMMITTED0); a.sload_to(A4, A3); a.beq(A4, ZERO, cmhalt)
    a.li(A3, S_COMMITTED1); a.sload_to(A4, A3); a.beq(A4, ZERO, cmhalt)
    a.li(A4, PH_ACT_P0); _sstore_imm(a, S_PHASE, A4); _set_deadline(a)
    a.halt()
    a.label(cmhalt)
    _set_deadline(a)                                        # restart the clock on each single fresh commit
    a.halt()

    # ---- FN_PLAY: P0 stays (ACT_P0 -> ACT_P1) ; P1 calls (ACT_P1 -> BOARD) ---------------------------
    a.label("play")
    _require_no_value(a)
    _sload(a, T2, S_PHASE)
    _which_player(a, S1)
    a.li(A2, PH_ACT_P0); a.bne(T2, A2, "play_try_p1$")     # P0's turn?
    a.bne(S1, ZERO, "revert")                              # only P0 acts in ACT_P0
    a.li(A4, PH_ACT_P1); _sstore_imm(a, S_PHASE, A4); _set_deadline(a)
    a.halt()
    a.label("play_try_p1$")
    a.li(A2, PH_ACT_P1); a.bne(T2, A2, "revert")
    a.li(A2, 1); a.bne(S1, A2, "revert")                  # only P1 calls in ACT_P1
    a.li(A4, PH_BOARD); _sstore_imm(a, S_PHASE, A4); _set_deadline(a)
    a.halt()

    # ---- FN_FOLD: actor folds -> opponent wins the pot ----------------------------------------------
    a.label("fold")
    _require_no_value(a)
    _sload(a, T2, S_PHASE)
    a.li(A2, PH_ACT_P0); a.beq(T2, A2, "fold_ok$")
    a.li(A2, PH_ACT_P1); a.beq(T2, A2, "fold_ok$")
    a.j("revert")
    a.label("fold_ok$")
    _which_player(a, S1)
    # the actor must be the player whose turn it is (ACT_P0 -> P0, ACT_P1 -> P1)
    a.li(A2, PH_ACT_P0); a.bne(T2, A2, "fold_p1turn$")
    a.bne(S1, ZERO, "revert")                              # P0's turn
    a.j("fold_settle$")
    a.label("fold_p1turn$")
    a.li(A2, 1); a.bne(S1, A2, "revert")                  # P1's turn
    a.label("fold_settle$")
    _other(a, S2, S1)                                      # winner = opponent
    a.j("settle_one$")

    # ---- FN_BOARD(c0..c4): BOTH players must post the SAME 5 community cards (BOARD -> SHOWDOWN) -------
    # The board is NOT committed on-chain, so a single party must not be able to choose it: only a player may
    # post (caller gate), the 5 cards must be in range and mutually distinct, and the phase advances ONLY when
    # the two players independently post the identical board. A disagreement / stall is resolved by the
    # PH_BOARD timeout, which voids the hand and refunds both stakes (no one can steal with a forged board).
    a.label("board")
    _require_no_value(a)
    _sload(a, A4, S_PHASE); a.li(A2, PH_BOARD); a.bne(A4, A2, "revert")
    _which_player(a, S1)                                   # caller must be a player; S1 = 0 (P0) / 1 (P1)
    # read + range-check the 5 proposed cards into SC_PROP
    a.li(T2, 0)
    a.label("bd_read$")
    a.li(A2, 5); a.bgeu(T2, A2, "bd_dist$")
    a.add(A2, S0, T2); a.lbu(T3, A2, 4)                    # card byte at calldata 4+i
    a.li(A2, 51); a.bltu(A2, T3, "revert")                 # 0..51
    a.li(A2, SC_PROP); a.add(A2, A2, T2); a.sb(T3, A2, 0)
    a.addi(T2, T2, 1); a.j("bd_read$")
    # the 5 proposed cards must be mutually distinct (rejects e.g. five identical cards)
    a.label("bd_dist$")
    a.li(T2, 0)
    a.label("bd_di$")
    a.li(A2, 5); a.bgeu(T2, A2, "bd_branch$")
    a.addi(T3, T2, 1)
    a.label("bd_dj$")
    a.li(A2, 5); a.bgeu(T3, A2, "bd_dinext$")
    a.li(A2, SC_PROP); a.add(A2, A2, T2); a.lbu(T4, A2, 0)
    a.li(A2, SC_PROP); a.add(A2, A2, T3); a.lbu(T5, A2, 0)
    a.beq(T4, T5, "revert")
    a.addi(T3, T3, 1); a.j("bd_dj$")
    a.label("bd_dinext$")
    a.addi(T2, T2, 1); a.j("bd_di$")
    a.label("bd_branch$")
    a.li(A2, 1); a.add(S2, S1, A2)                         # S2 = my proposer code (1=P0, 2=P1)
    _sload(a, T6, S_BOARDER)                               # current boarder code (0 = none yet)
    a.beq(T6, ZERO, "bd_first$")                           # first to post -> record the proposal
    a.beq(T6, S2, "revert")                                # same player re-posting -> reject
    # second poster: the proposal must match the stored board EXACTLY, else reject (resolve via timeout void)
    a.li(T2, 0)
    a.label("bd_cmp$")
    a.li(A2, 5); a.bgeu(T2, A2, "bd_agree$")
    a.li(A2, SC_PROP); a.add(A2, A2, T2); a.lbu(T4, A2, 0)
    a.li(A3, TAG_BOARD); a.add(A3, A3, T2); a.sload_to(T5, A3)
    a.bne(T4, T5, "revert")
    a.addi(T2, T2, 1); a.j("bd_cmp$")
    a.label("bd_agree$")
    a.li(A4, PH_SHOWDOWN); _sstore_imm(a, S_PHASE, A4); _set_deadline(a)
    a.halt()
    a.label("bd_first$")
    a.li(T2, 0)
    a.label("bd_store$")
    a.li(A2, 5); a.bgeu(T2, A2, "bd_stored$")
    a.li(A2, SC_PROP); a.add(A2, A2, T2); a.lbu(T3, A2, 0)
    a.li(A3, TAG_BOARD); a.add(A3, A3, T2); a.sstore(A3, T3)
    a.addi(T2, T2, 1); a.j("bd_store$")
    a.label("bd_stored$")
    a.mv(A4, S2); _sstore_imm(a, S_BOARDER, A4)            # record who proposed
    _set_deadline(a)                                       # give the other player a fresh window to match
    a.halt()

    # ---- FN_REVEAL(...) : verify commit, gather best-5, rank; both revealed -> settle ----------------
    a.label("reveal")
    _require_no_value(a)
    _sload(a, A4, S_PHASE); a.li(A2, PH_SHOWDOWN); a.bne(A4, A2, "revert")
    _which_player(a, S1)
    # my reveal flag slot -> S3 ; require not already revealed
    rv1 = a._uniq("rv_p1")
    a.li(S3, S_REVEAL0); a.beq(S1, ZERO, rv1); a.li(S3, S_REVEAL1); a.label(rv1)
    a.mv(A3, S3); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")
    # build SHA256 preimage hole0|hole1|nonce at SC_PRE, hash, compare to my commit
    a.lbu(T3, S0, 4); a.li(A2, 51); a.bltu(A2, T3, "revert"); a.li(A2, SC_PRE); a.sb(T3, A2, 0)
    a.lbu(T4, S0, 5); a.li(A2, 51); a.bltu(A2, T4, "revert"); a.li(A2, SC_PRE); a.sb(T4, A2, 1)
    a.li(T5, 0)
    a.label("rv_nonce$")
    a.li(A2, 32); a.bgeu(T5, A2, "rv_hash$")
    a.add(A2, S0, T5); a.lbu(A6, A2, 6)                    # nonce byte at calldata 6+i
    a.li(A2, SC_PRE + 2); a.add(A2, A2, T5); a.sb(A6, A2, 0)
    a.addi(T5, T5, 1); a.j("rv_nonce$")
    a.label("rv_hash$")
    a.li(A0, SC_PRE); a.li(A1, 34); a.li(A2, SC_SHA); a.syscall(SYS_SHA256)
    # my commit tag base -> S2
    rvc1 = a._uniq("rv_c1")
    a.li(S2, TAG_COMMIT0); a.beq(S1, ZERO, rvc1); a.li(S2, TAG_COMMIT1); a.label(rvc1)
    a.li(T5, 0)
    a.label("rv_cmp$")
    a.li(A2, 8); a.bgeu(T5, A2, "rv_ok$")
    a.mv(A3, S2); a.or_(A3, A3, T5); a.sload_to(T6, A3)    # committed word
    a.li(A2, SC_SHA); a.slli(A4, T5, 2); a.add(A4, A2, A4)
    a.lbu(A2, A4, 0); a.slli(A2, A2, 8); a.lbu(A6, A4, 1); a.or_(A2, A2, A6); a.slli(A2, A2, 8)
    a.lbu(A6, A4, 2); a.or_(A2, A2, A6); a.slli(A2, A2, 8); a.lbu(A6, A4, 3); a.or_(A2, A2, A6)
    a.bne(T6, A2, "revert")
    a.addi(T5, T5, 1); a.j("rv_cmp$")
    a.label("rv_ok$")
    # store the two revealed hole cards (hole tag base -> S2)
    rvh1 = a._uniq("rv_h1")
    a.li(S2, TAG_HOLE0); a.beq(S1, ZERO, rvh1); a.li(S2, TAG_HOLE1); a.label(rvh1)
    a.lbu(T3, S0, 4); a.lbu(T4, S0, 5)
    a.mv(A3, S2); a.sstore(A3, T3); a.mv(A3, S2); a.li(A2, 1); a.or_(A3, A3, A2); a.sstore(A3, T4)
    # gather the 5 chosen cards (indices at calldata 38..42) into SC_CARDS, distinct & in 0..6
    _gather5(a, S2)                                        # S2 = my hole tag base
    # rank the 5 cards (inlined block); result in A0
    a.j("rank5_entry$")
    a.label("rank_back$")
    a.mv(S2, A0)                                           # S2 = my rank
    rrk1 = a._uniq("rv_rank1")
    a.li(A3, S_RANK0); a.beq(S1, ZERO, rrk1); a.li(A3, S_RANK1); a.label(rrk1)
    a.sstore(A3, S2)
    a.li(A4, 1); a.mv(A3, S3); a.sstore(A3, A4)            # set my reveal flag
    a.li(A3, S_REVEAL0); a.sload_to(A4, A3); a.beq(A4, ZERO, "rv_wait$")
    a.li(A3, S_REVEAL1); a.sload_to(A4, A3); a.beq(A4, ZERO, "rv_wait$")
    a.j("showdown_settle$")
    a.label("rv_wait$")
    _set_deadline(a)
    a.halt()

    # ---- FN_TIMEOUT: after the deadline, anyone may settle the stalled phase so escrow can never wedge ----
    a.label("timeout")
    _require_no_value(a)
    _sload(a, T2, S_PHASE)
    a.li(A2, PH_WAIT_P1); a.beq(T2, A2, "to_wait$")        # WAIT_P1: refund P0 if no opponent ever joined
    a.li(A2, PH_COMMIT); a.bltu(T2, A2, "revert")          # nothing to time out before a stake is matched
    a.li(A2, PH_DONE); a.bgeu(T2, A2, "revert")
    a.number(A4); a.li(A3, S_DEADLINE); a.sload_to(A2, A3); a.bgeu(A2, A4, "revert")   # deadline passed?
    a.li(A2, PH_COMMIT); a.beq(T2, A2, "to_commit$")       # commit overdue -> committer wins / split refund
    a.li(A2, PH_ACT_P0); a.beq(T2, A2, "to_p0turn$")       # P0 overdue -> P1 wins
    a.li(A2, PH_ACT_P1); a.beq(T2, A2, "to_p1turn$")       # P1 overdue -> P0 wins
    a.li(A2, PH_BOARD); a.beq(T2, A2, "to_board$")         # board not agreed in time -> void + refund both
    # PH_SHOWDOWN: whoever revealed wins; if neither revealed, refund both (split)
    a.li(A3, S_REVEAL0); a.sload_to(A4, A3); a.bne(A4, ZERO, "to_p0rev$")
    a.li(A3, S_REVEAL1); a.sload_to(A4, A3); a.beq(A4, ZERO, "to_split$")
    a.li(S2, 1); a.j("settle_one$")
    a.label("to_p0rev$")
    a.li(A3, S_REVEAL1); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")   # both revealed -> use showdown
    a.li(S2, 0); a.j("settle_one$")
    a.label("to_p0turn$"); a.li(S2, 1); a.j("settle_one$")
    a.label("to_p1turn$"); a.li(S2, 0); a.j("settle_one$")
    # board / showdown-both-abandoned / commit-neither: void the hand and refund each player their buyin
    a.label("to_board$")
    a.label("to_split$")
    a.li(A4, PH_DONE); _sstore_imm(a, S_PHASE, A4); a.li(A4, 1); _sstore_imm(a, S_DONEFLAG, A4)
    a.li(S2, 0); _pay_pot(a, S2, int(buyin))
    a.li(S2, 1); _pay_pot(a, S2, int(buyin))
    a.halt()
    # commit overdue: the lone committer forfeits nothing and takes the pot; neither committed -> split refund
    a.label("to_commit$")
    a.li(A3, S_COMMITTED0); a.sload_to(T3, A3)             # T3 != 0 iff P0 committed
    a.li(A3, S_COMMITTED1); a.sload_to(T4, A3)             # T4 != 0 iff P1 committed
    a.bne(T3, ZERO, "to_cm_p0in$")
    a.beq(T4, ZERO, "to_split$")                           # neither committed -> refund both
    a.li(S2, 1); a.j("settle_one$")                        # only P1 committed -> P1 wins POT
    a.label("to_cm_p0in$")
    a.bne(T4, ZERO, "revert")                              # both committed is impossible here (would advance)
    a.li(S2, 0); a.j("settle_one$")                        # only P0 committed -> P0 wins POT
    # WAIT_P1 overdue: refund P0's single staked buyin and close the table (permissionless after deadline)
    a.label("to_wait$")
    a.number(A4); a.li(A3, S_DEADLINE); a.sload_to(A2, A3); a.bgeu(A2, A4, "revert")   # deadline passed?
    a.li(A4, PH_DONE); _sstore_imm(a, S_PHASE, A4); a.li(A4, 1); _sstore_imm(a, S_DONEFLAG, A4)
    a.li(S2, 0); _pay_pot(a, S2, int(buyin))               # custody == one buyin in WAIT_P1
    a.halt()

    # ---- settlement --------------------------------------------------------------------------------
    a.label("settle_one$")                                 # pay whole POT to player S2
    a.li(A4, PH_DONE); _sstore_imm(a, S_PHASE, A4); a.li(A4, 1); _sstore_imm(a, S_DONEFLAG, A4)
    _pay_pot(a, S2, POT)
    a.halt()

    a.label("showdown_settle$")
    a.li(A4, PH_DONE); _sstore_imm(a, S_PHASE, A4); a.li(A4, 1); _sstore_imm(a, S_DONEFLAG, A4)
    _sload(a, T3, S_RANK0); _sload(a, T4, S_RANK1)
    a.bltu(T3, T4, "sd_p1$"); a.bltu(T4, T3, "sd_p0$")
    # tie -> split: buyin to each
    a.li(S2, 0); _pay_pot(a, S2, int(buyin))
    a.li(S2, 1); _pay_pot(a, S2, int(buyin))
    a.halt()
    a.label("sd_p0$"); a.li(S2, 0); _pay_pot(a, S2, POT); a.halt()
    a.label("sd_p1$"); a.li(S2, 1); _pay_pot(a, S2, POT); a.halt()

    # ---- the 5-card hand ranker (inlined; reached by `j rank5_entry$`, returns by `j rank_back$`) ----
    _rank5(a)

    a.label("revert")
    a.raw(0)
    return a.assemble()


def _gather5(a, hole_tag_reg):
    """Read 5 indices (calldata 38..42, each 0..6), map to cards from [hole0,hole1,board0..4], write the 5
    card bytes to SC_CARDS[0..4], then require those 5 CARD VALUES to be pairwise distinct. Comparing the
    resolved cards (not just the indices) rejects any physical card reused within the ranked hand — e.g. a
    revealed hole card that duplicates a board card — since each byte 0..51 is one unique card. Reverts on a
    bad index or a duplicate card. Uses T2..T6, A2..A4."""
    a.li(T2, 0)
    a.label("g5_loop$")
    a.li(A2, 5); a.bgeu(T2, A2, "g5_dist$")
    a.add(A2, S0, T2); a.lbu(T3, A2, 38)                  # idx_i
    a.li(A2, 6); a.bltu(A2, T3, "revert")                 # 0..6
    a.li(A2, 2); a.bltu(T3, A2, "g5_hole$")
    a.addi(A4, T3, -2); a.li(A3, TAG_BOARD); a.add(A3, A3, A4); a.sload_to(T4, A3)   # board[idx-2]
    a.j("g5_put$")
    a.label("g5_hole$")
    a.mv(A3, hole_tag_reg); a.add(A3, A3, T3); a.sload_to(T4, A3)                    # hole[idx]
    a.label("g5_put$")
    a.li(A2, SC_CARDS); a.add(A2, A2, T2); a.sb(T4, A2, 0)
    a.addi(T2, T2, 1); a.j("g5_loop$")
    a.label("g5_dist$")
    a.li(T2, 0)
    a.label("g5_di$")
    a.li(A2, 5); a.bgeu(T2, A2, "g5_done$")
    a.addi(T3, T2, 1)
    a.label("g5_dj$")
    a.li(A2, 5); a.bgeu(T3, A2, "g5_dinext$")
    a.li(A2, SC_CARDS); a.add(A2, A2, T2); a.lbu(T4, A2, 0)
    a.li(A2, SC_CARDS); a.add(A2, A2, T3); a.lbu(T5, A2, 0)
    a.beq(T4, T5, "revert")                               # duplicate physical card -> illegal hand
    a.addi(T3, T3, 1); a.j("g5_dj$")
    a.label("g5_dinext$")
    a.addi(T2, T2, 1); a.j("g5_di$")
    a.label("g5_done$")


def _rank5(a):
    """Rank the 5 card bytes at SC_CARDS[0..4] -> A0 = category<<20 | tiebreak. category 8..0
    (straight-flush..high card). Entered by `j rank5_entry$`; returns by `j rank_back$`. Card=0..51,
    rank=card%13 (0=2..12=A), suit=card//13.

    Count-based (correct poker tiebreaks): builds counts[0..12], detects flush + the highest straight
    (incl. the A-2-3-4-5 wheel), classifies from the multiplicity multiset, and packs a tiebreak that orders
    ranks by (count desc, rank desc) — so pair/trips/quads ranks dominate the kickers. Straights/straight-
    flushes pack just the high-card rank (wheel high = 5). Uses ONLY T0..T6 + A0/A2..A6 (no S registers, so
    the caller's S0..S3 survive) and has no syscalls."""
    a.label("rank5_entry$")
    # zero counts[0..12]
    a.li(T0, 0)
    a.label("r5_zc$")
    a.li(A2, 13); a.bgeu(T0, A2, "r5_zc_done$")
    a.li(A2, SC_CNT); a.add(A2, A2, T0); a.sb(ZERO, A2, 0)
    a.addi(T0, T0, 1); a.j("r5_zc$")
    a.label("r5_zc_done$")
    # scan 5 cards: counts[rank]++ ; flush tracking (T1=flush flag, T2=firstsuit)
    a.li(T0, 0); a.li(T1, 1); a.li(T2, -1)
    a.label("r5_scan$")
    a.li(A2, 5); a.bgeu(T0, A2, "r5_counts$")
    a.li(A2, SC_CARDS); a.add(A2, A2, T0); a.lbu(A4, A2, 0)   # A4 = card
    a.li(A5, 0); a.mv(A6, A4)                                 # A5 = suit, A6 = work
    a.label("r5_div$")
    a.li(A2, 13); a.bltu(A6, A2, "r5_divdone$")
    a.addi(A6, A6, -13); a.addi(A5, A5, 1); a.j("r5_div$")
    a.label("r5_divdone$")                                    # A6 = rank, A5 = suit
    a.li(A2, SC_CNT); a.add(A2, A2, A6); a.lbu(A3, A2, 0); a.addi(A3, A3, 1); a.sb(A3, A2, 0)
    a.li(A2, -1); a.bne(T2, A2, "r5_sc_cmp$")
    a.mv(T2, A5); a.j("r5_sc_next$")
    a.label("r5_sc_cmp$")
    a.beq(T2, A5, "r5_sc_next$"); a.li(T1, 0)                 # suit mismatch -> not a flush
    a.label("r5_sc_next$")
    a.addi(T0, T0, 1); a.j("r5_scan$")
    # tally multiplicities: T2=num2, T3=num3, T4=num4 (T1 still = flush)
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
    # straight high rank -> T5 (-1 if none). window i=0..8 of 5 consecutive counts==1 (highest wins); wheel.
    a.label("r5_straight$")
    a.li(T5, -1)
    a.li(T0, 0)
    a.label("r5_st$")
    a.li(A2, 8); a.bltu(A2, T0, "r5_st_wheel$")
    a.li(T6, 0); a.li(A5, 1)                                  # T6 = k, A5 = all-ones flag
    a.label("r5_st_k$")
    a.li(A2, 5); a.bgeu(T6, A2, "r5_st_chk$")
    a.li(A2, SC_CNT); a.add(A2, A2, T0); a.add(A2, A2, T6); a.lbu(A4, A2, 0)
    a.li(A2, 1); a.beq(A4, A2, "r5_st_kok$"); a.li(A5, 0)
    a.label("r5_st_kok$")
    a.addi(T6, T6, 1); a.j("r5_st_k$")
    a.label("r5_st_chk$")
    a.beq(A5, ZERO, "r5_st_next$")
    a.addi(T5, T0, 4)                                         # all five present -> straight, high = i+4
    a.label("r5_st_next$")
    a.addi(T0, T0, 1); a.j("r5_st$")
    a.label("r5_st_wheel$")
    a.li(A2, -1); a.bne(T5, A2, "r5_cat$")                    # a normal straight already found (higher)
    # wheel: counts[12],counts[0..3] all == 1
    a.li(A2, SC_CNT); a.lbu(A4, A2, 12); a.li(A3, 1); a.bne(A4, A3, "r5_cat$")
    a.lbu(A4, A2, 0); a.bne(A4, A3, "r5_cat$")
    a.lbu(A4, A2, 1); a.bne(A4, A3, "r5_cat$")
    a.lbu(A4, A2, 2); a.bne(A4, A3, "r5_cat$")
    a.lbu(A4, A2, 3); a.bne(A4, A3, "r5_cat$")
    a.li(T5, 3)                                              # wheel: high card is the 5 (rank 3)
    # ---- category -> A0 ----
    a.label("r5_cat$")
    a.li(A0, 0)
    a.li(A2, -1); a.beq(T5, A2, "r5_nostr$")                  # no straight
    a.beq(T1, ZERO, "r5_str$")                                # straight, not flush
    a.li(A0, 8); a.j("r5_tb$")                                # straight flush
    a.label("r5_str$"); a.li(A0, 4); a.j("r5_tb$")
    a.label("r5_nostr$")
    a.bne(T4, ZERO, "r5_quads$")                              # num4 -> quads
    a.beq(T3, ZERO, "r5_no3$")                                # no trips
    a.beq(T2, ZERO, "r5_trips$")                              # trips, no pair -> three of a kind
    a.li(A0, 6); a.j("r5_tb$")                                # full house
    a.label("r5_trips$"); a.li(A0, 3); a.j("r5_tb$")
    a.label("r5_no3$")
    a.beq(T1, ZERO, "r5_pairs$")
    a.li(A0, 5); a.j("r5_tb$")                                # flush
    a.label("r5_pairs$")
    a.li(A2, 2); a.beq(T2, A2, "r5_2pair$")
    a.li(A2, 1); a.beq(T2, A2, "r5_1pair$")
    a.li(A0, 0); a.j("r5_tb$")                                # high card
    a.label("r5_2pair$"); a.li(A0, 2); a.j("r5_tb$")
    a.label("r5_1pair$"); a.li(A0, 1); a.j("r5_tb$")
    a.label("r5_quads$"); a.li(A0, 7)
    # ---- tiebreak -> A0 ----
    a.label("r5_tb$")
    a.slli(A0, A0, 20)
    a.li(A2, -1); a.beq(T5, A2, "r5_tb_counts$")              # not a straight -> count-ordered tiebreak
    a.or_(A0, A0, T5); a.j("rank_back$")                      # straight/SF: high-card rank suffices
    a.label("r5_tb_counts$")
    a.li(T6, 0)                                              # tb accumulator
    a.li(T5, 4)                                              # c = 4..1
    a.label("r5_tb_c$")
    a.beq(T5, ZERO, "r5_tb_done$")
    a.li(T0, 12)                                            # r = 12..0
    a.label("r5_tb_r$")
    a.li(A2, SC_CNT); a.add(A2, A2, T0); a.lbu(A4, A2, 0)
    a.bne(A4, T5, "r5_tb_rnext$")
    a.slli(T6, T6, 4); a.or_(T6, T6, T0)                    # append rank r (once, in (count,rank) order)
    a.label("r5_tb_rnext$")
    a.beq(T0, ZERO, "r5_tb_cnext$")
    a.addi(T0, T0, -1); a.j("r5_tb_r$")
    a.label("r5_tb_cnext$")
    a.addi(T5, T5, -1); a.j("r5_tb_c$")
    a.label("r5_tb_done$")
    a.or_(A0, A0, T6)
    a.j("rank_back$")


# --- read helpers (tests / relay) ---
def addr_word_key(player, w):
    return (TAG_P0ADDR if player == 0 else TAG_P1ADDR) | w


def board_key(i):
    return TAG_BOARD | i


def commit_key(player, w):
    return (TAG_COMMIT0 if player == 0 else TAG_COMMIT1) | w


def hole_key(player, j):
    return (TAG_HOLE0 if player == 0 else TAG_HOLE1) | j
