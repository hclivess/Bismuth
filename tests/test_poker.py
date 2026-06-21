"""Heads-up all-in Texas Hold'em (contracts/poker.py) — the chain as trustless referee: escrow + commit-
reveal hole cards + on-chain hand evaluation + settlement.

OFFLINE tests drive the assembled bytecode through bismuth_riscv.execute with a custody-tracking harness.
The crux is the on-chain 5-card hand evaluator: a Python reference (`rank5_ref`) recomputes the exact same
category<<20|tiebreak integer, and we check the contract's stored rank against it for every hand category
and several same-category tiebreaks (including the A-2-3-4-5 wheel and count-ordered kickers). We also drive
a full hand to showdown and assert the better hand is paid, plus fold/commit-mismatch guards.

Run with: python3 -m pytest tests/test_poker.py -v
"""
import hashlib

import bismuth_riscv as rv
import poker

UNIT = 100_000_000
BUYIN = 1 * UNIT
P0, P1 = 0xAAAA0001, 0xBBBB0002


def addr28(fold):
    return bytes(24) + (fold & 0xFFFFFFFF).to_bytes(4, "big")


def be4(x):
    return (x & 0xFFFFFFFF).to_bytes(4, "big")


def card(rank, suit):
    """card int 0..51, rank 0..12 (0=2 .. 12=Ace), suit 0..3."""
    return suit * 13 + rank


# ---- Python reference for the contract's count-based evaluator (must match exactly) ----
def rank5_ref(cards):
    cnt = [0] * 13
    suits = set()
    for c in cards:
        cnt[c % 13] += 1
        suits.add(c // 13)
    flush = len(suits) == 1
    num2 = sum(1 for x in cnt if x == 2)
    num3 = sum(1 for x in cnt if x == 3)
    num4 = sum(1 for x in cnt if x == 4)
    sh = -1
    for i in range(0, 9):
        if all(cnt[i + k] == 1 for k in range(5)):
            sh = i + 4
    if sh == -1 and cnt[12] == 1 and cnt[0] == 1 and cnt[1] == 1 and cnt[2] == 1 and cnt[3] == 1:
        sh = 3
    if sh != -1:
        cat = 8 if flush else 4
    elif num4:
        cat = 7
    elif num3 and num2:
        cat = 6
    elif num3:
        cat = 3
    elif flush:
        cat = 5
    elif num2 == 2:
        cat = 2
    elif num2 == 1:
        cat = 1
    else:
        cat = 0
    if sh != -1:
        tb = sh
    else:
        tb = 0
        for c in (4, 3, 2, 1):
            for r in range(12, -1, -1):
                if cnt[r] == c:
                    tb = (tb << 4) | r
    return (cat << 20) | tb


class Poker:
    """Offline harness: run the poker bytecode with persistent storage + tracked custody."""

    def __init__(self, p0=P0, buyin=BUYIN):
        self.code = poker.build(p0, buyin)
        self.st = {}
        self.custody = 0
        self.height = 100

    def call(self, sel, caller, tail=b"", value=0):
        return rv.execute(self.code, calldata=be4(sel) + tail, caller=caller, callvalue=value,
                          storage=dict(self.st), block_height=self.height, self_balance=self.custody + value)

    def ok(self, sel, caller, tail=b"", value=0):
        r = self.call(sel, caller, tail, value)
        assert r.success, "expected success, got revert (sel %d)" % sel
        self.st = r.storage
        self.custody += value
        for _to, amt in r.transfers:
            self.custody -= amt
        return r

    def reverts(self, sel, caller, tail=b"", value=0):
        assert not self.call(sel, caller, tail, value).success, "expected revert (sel %d)" % sel

    def slot(self, s):
        return self.st.get(s, 0)


def commit_of(h0, h1, nonce):
    return hashlib.sha256(bytes([h0, h1]) + nonce).digest()


def _start(d):
    """stake0 + join + both commit -> phase ACT_P0. Returns (noncesP0, noncesP1) holders set per test."""
    d.ok(poker.FN_STAKE0, P0, addr28(P0), value=BUYIN)
    d.ok(poker.FN_JOIN, P1, addr28(P1), value=BUYIN)
    assert d.custody == 2 * BUYIN


def _commit(d, player, h0, h1, nonce):
    caller = P0 if player == 0 else P1
    d.ok(poker.FN_COMMIT, caller, commit_of(h0, h1, nonce))


def _board(d, board):
    """Post the agreed 5-card board. BOTH players must independently post the identical cards before the
    table advances to SHOWDOWN (no single party can choose a self-favoring board)."""
    bb = bytes(board)
    r = d.ok(poker.FN_BOARD, P0, bb)
    assert d.slot(poker.S_PHASE) == poker.PH_BOARD, "one post must not yet advance to SHOWDOWN"
    d.ok(poker.FN_BOARD, P1, bb)
    assert d.slot(poker.S_PHASE) == poker.PH_SHOWDOWN, "matching second post advances to SHOWDOWN"
    return r


# ------------------------------------------------------- evaluator (the critical correctness piece)
def _eval_via_contract(target5, board_fill=None):
    """Drive a fresh game so P0's chosen five cards == target5, then return the contract's stored S_RANK0.
    P0 hole = target5[0:2]; board = target5[2:5] + 2 fillers; P0 reveals indices [0,1,2,3,4]. The two board
    fillers (never chosen by P0) are picked distinct from every card in the hand so the 5-card board is a
    legal, duplicate-free board (FN_BOARD now enforces board-internal distinctness)."""
    d = Poker()
    _start(d)
    used = set(target5)
    fillers = [c for c in range(52) if c not in used][:2]
    nonce0 = bytes(range(32))
    _commit(d, 0, target5[0], target5[1], nonce0)
    _commit(d, 1, card(0, 0), card(1, 0), bytes([7]) * 32)     # P1 arbitrary commit
    d.ok(poker.FN_PLAY, P0)                                     # P0 plays
    d.ok(poker.FN_PLAY, P1)                                     # P1 calls -> board phase
    board = [target5[2], target5[3], target5[4], fillers[0], fillers[1]]
    _board(d, board)
    tail = bytes([target5[0], target5[1]]) + nonce0 + bytes([0, 1, 2, 3, 4])
    d.ok(poker.FN_REVEAL, P0, tail)
    return d.slot(poker.S_RANK0)


# distinct fillers that won't collide with the target categories we test (high spades not used below)
FILL = (card(7, 3), card(9, 3))

CATEGORY_HANDS = {
    "straight_flush": [card(8, 0), card(9, 0), card(10, 0), card(11, 0), card(12, 0)],   # 10-A hearts
    "quads":          [card(5, 0), card(5, 1), card(5, 2), card(5, 3), card(12, 0)],      # 7777 + A
    "full_house":     [card(5, 0), card(5, 1), card(5, 2), card(8, 0), card(8, 1)],       # 777 + TT
    "flush":          [card(2, 1), card(5, 1), card(8, 1), card(10, 1), card(12, 1)],     # mixed ranks, 1 suit
    "straight":       [card(4, 0), card(5, 1), card(6, 2), card(7, 3), card(8, 0)],       # 6-T mixed
    "trips":          [card(5, 0), card(5, 1), card(5, 2), card(8, 0), card(11, 1)],      # 777 + T + K
    "two_pair":       [card(5, 0), card(5, 1), card(8, 0), card(8, 1), card(11, 0)],      # 77 TT K
    "pair":           [card(5, 0), card(5, 1), card(8, 0), card(10, 0), card(12, 1)],     # 77 + T Q A
    "high":           [card(2, 0), card(5, 1), card(8, 2), card(10, 0), card(12, 1)],     # no pair/flush/straight
    "wheel":          [card(12, 0), card(0, 1), card(1, 2), card(2, 3), card(3, 0)],      # A-2-3-4-5
}


def test_evaluator_matches_reference_all_categories():
    for name, hand in CATEGORY_HANDS.items():
        got = _eval_via_contract(hand, FILL)
        exp = rank5_ref(hand)
        assert got == exp, "%s: contract rank %d != reference %d" % (name, got, exp)


def test_evaluator_category_ordering():
    # the on-chain ranks must order the categories correctly
    order = ["high", "pair", "two_pair", "trips", "straight", "flush", "full_house", "quads", "straight_flush"]
    ranks = [_eval_via_contract(CATEGORY_HANDS[n], FILL) for n in order]
    assert ranks == sorted(ranks), "category ranks not monotonically increasing: %r" % ranks
    # the wheel must rank below the lowest non-wheel straight (6-high)
    assert _eval_via_contract(CATEGORY_HANDS["wheel"], FILL) < _eval_via_contract(CATEGORY_HANDS["straight"], FILL)


def test_evaluator_tiebreaks():
    # pair of Kings, kicker A-Q-J  >  pair of Kings, kicker A-Q-9 (count-ordered: pair dominates, then kickers)
    kk_hi = [card(11, 0), card(11, 1), card(12, 0), card(10, 0), card(9, 0)]
    kk_lo = [card(11, 0), card(11, 1), card(12, 0), card(10, 0), card(7, 0)]
    assert _eval_via_contract(kk_hi, FILL) > _eval_via_contract(kk_lo, FILL)
    # pair of Aces beats pair of Kings regardless of kickers
    aa = [card(12, 0), card(12, 1), card(2, 0), card(3, 0), card(4, 0)]
    assert _eval_via_contract(aa, FILL) > _eval_via_contract(kk_hi, FILL)
    # two pair: AA KK  >  AA QQ
    aakk = [card(12, 0), card(12, 1), card(11, 0), card(11, 1), card(2, 0)]
    aaqq = [card(12, 0), card(12, 1), card(10, 0), card(10, 1), card(2, 0)]
    assert _eval_via_contract(aakk, FILL) > _eval_via_contract(aaqq, FILL)
    # higher straight beats lower straight
    s_hi = [card(8, 0), card(9, 1), card(10, 2), card(11, 3), card(12, 0)]   # 10-A
    s_lo = [card(4, 0), card(5, 1), card(6, 2), card(7, 3), card(8, 0)]      # 6-T
    assert _eval_via_contract(s_hi, FILL) > _eval_via_contract(s_lo, FILL)


# ------------------------------------------------------- full flow + settlement
def test_full_hand_pays_better_hand():
    d = Poker()
    _start(d)
    # P0 gets a pair of Aces, P1 gets king-high — P0 should win the 2*BUYIN pot.
    n0, n1 = bytes(range(32)), bytes(reversed(range(32)))
    p0_hole = (card(12, 0), card(12, 1))      # AA
    p1_hole = (card(11, 0), card(9, 1))        # K, T
    _commit(d, 0, p0_hole[0], p0_hole[1], n0)
    _commit(d, 1, p1_hole[0], p1_hole[1], n1)
    d.ok(poker.FN_PLAY, P0)
    d.ok(poker.FN_PLAY, P1)
    board = [card(2, 2), card(5, 3), card(7, 0), card(0, 1), card(3, 2)]   # nothing that pairs either
    _board(d, board)
    # P0 plays AA + 3 board kickers (indices 0,1,2,3,4); P1 plays K + board
    d.ok(poker.FN_REVEAL, P0, bytes([p0_hole[0], p0_hole[1]]) + n0 + bytes([0, 1, 2, 3, 4]))
    r = d.ok(poker.FN_REVEAL, P1, bytes([p1_hole[0], p1_hole[1]]) + n1 + bytes([0, 1, 2, 3, 4]))
    assert (int.from_bytes(addr28(P0), "big"), 2 * BUYIN) in r.transfers, "P0 (pair of aces) wins the pot"
    assert d.custody == 0 and d.slot(poker.S_PHASE) == poker.PH_DONE


def test_tie_splits_the_pot():
    d = Poker()
    _start(d)
    # both play the board (an A-high straight on board), neither hole improves -> identical best 5 -> split
    n0, n1 = bytes([1]) * 32, bytes([2]) * 32
    _commit(d, 0, card(0, 0), card(1, 0), n0)     # 2,3 (won't be used in the chosen 5)
    _commit(d, 1, card(0, 1), card(1, 1), n1)
    d.ok(poker.FN_PLAY, P0); d.ok(poker.FN_PLAY, P1)
    board = [card(8, 0), card(9, 1), card(10, 2), card(11, 3), card(12, 0)]   # 10-J-Q-K-A straight
    _board(d, board)
    # both choose the 5 board cards (indices 2,3,4,5,6)
    d.ok(poker.FN_REVEAL, P0, bytes([card(0, 0), card(1, 0)]) + n0 + bytes([2, 3, 4, 5, 6]))
    r = d.ok(poker.FN_REVEAL, P1, bytes([card(0, 1), card(1, 1)]) + n1 + bytes([2, 3, 4, 5, 6]))
    assert (int.from_bytes(addr28(P0), "big"), BUYIN) in r.transfers
    assert (int.from_bytes(addr28(P1), "big"), BUYIN) in r.transfers
    assert d.custody == 0


def test_fold_awards_pot_to_opponent():
    d = Poker(); _start(d)
    _commit(d, 0, card(0, 0), card(1, 0), bytes(32))
    _commit(d, 1, card(2, 0), card(3, 0), bytes(32))
    r = d.ok(poker.FN_FOLD, P0)                                # P0 folds in ACT_P0 -> P1 wins
    assert (int.from_bytes(addr28(P1), "big"), 2 * BUYIN) in r.transfers and d.custody == 0


def test_commit_mismatch_reverts():
    d = Poker(); _start(d)
    n0 = bytes(range(32))
    _commit(d, 0, card(12, 0), card(12, 1), n0)
    _commit(d, 1, card(0, 0), card(1, 0), bytes(32))
    d.ok(poker.FN_PLAY, P0); d.ok(poker.FN_PLAY, P1)
    _board(d, [card(2, 2), card(5, 3), card(7, 0), card(0, 1), card(3, 2)])
    # reveal with the WRONG hole cards (not matching the commitment) must revert
    d.reverts(poker.FN_REVEAL, P0, bytes([card(11, 0), card(11, 1)]) + n0 + bytes([0, 1, 2, 3, 4]))
    # reveal with a duplicate index (card used twice) must revert
    d.reverts(poker.FN_REVEAL, P0, bytes([card(12, 0), card(12, 1)]) + n0 + bytes([0, 1, 2, 2, 4]))
    # correct reveal succeeds
    d.ok(poker.FN_REVEAL, P0, bytes([card(12, 0), card(12, 1)]) + n0 + bytes([0, 1, 2, 3, 4]))


def test_join_and_stake_guards():
    d = Poker()
    d.reverts(poker.FN_STAKE0, P1, addr28(P1), value=BUYIN)          # only baked-in P0 may stake0
    d.reverts(poker.FN_STAKE0, P0, addr28(P0), value=BUYIN - 1)      # must attach exactly BUYIN
    d.ok(poker.FN_STAKE0, P0, addr28(P0), value=BUYIN)
    d.reverts(poker.FN_JOIN, P0, addr28(P0), value=BUYIN)            # P0 cannot also be P1
    d.ok(poker.FN_JOIN, P1, addr28(P1), value=BUYIN)
    d.reverts(poker.FN_JOIN, 0xCCCC0003, addr28(0xCCCC0003), value=BUYIN)   # table full


def test_build_validates():
    import pytest
    with pytest.raises(ValueError):
        poker.build(P0, 0)
    with pytest.raises(ValueError):
        poker.build(P0, 2 ** 32)


def _to0(addr):
    return int.from_bytes(addr28(addr), "big")


# ------------------------------------------------------- audit regressions (escrow can never wedge / be stolen)
def test_wait_p1_timeout_refunds_p0():
    # (audit) P0 stakes and nobody ever joins: after the deadline P0's lone buyin is refundable, not locked.
    d = Poker()
    d.ok(poker.FN_STAKE0, P0, addr28(P0), value=BUYIN)
    assert d.custody == BUYIN and d.slot(poker.S_PHASE) == poker.PH_WAIT_P1
    d.reverts(poker.FN_TIMEOUT, P0)                       # before the deadline: nothing to time out
    d.height = 1_000
    r = d.ok(poker.FN_TIMEOUT, 0xCAFE)                    # permissionless after the deadline; refund goes to P0
    assert (_to0(P0), BUYIN) in r.transfers
    assert d.custody == 0 and d.slot(poker.S_PHASE) == poker.PH_DONE


def test_commit_timeout_committer_takes_pot():
    # (audit) both staked, only ONE commits, the other stalls: the committer claims the pot after the deadline.
    for committer in (0, 1):
        d = Poker(); _start(d)
        _commit(d, committer, card(12, 0), card(12, 1), bytes(range(32)))
        assert d.slot(poker.S_PHASE) == poker.PH_COMMIT
        d.reverts(poker.FN_TIMEOUT, P0)                  # before the deadline
        d.height = 1_000
        r = d.ok(poker.FN_TIMEOUT, P1)
        winner = P0 if committer == 0 else P1
        assert (_to0(winner), 2 * BUYIN) in r.transfers
        assert d.custody == 0 and d.slot(poker.S_PHASE) == poker.PH_DONE


def test_commit_timeout_neither_splits():
    # (audit) both staked, NEITHER commits: after the deadline each stake is refunded (no permanent lock).
    d = Poker(); _start(d)
    d.height = 1_000
    r = d.ok(poker.FN_TIMEOUT, P0)
    assert (_to0(P0), BUYIN) in r.transfers and (_to0(P1), BUYIN) in r.transfers
    assert d.custody == 0 and d.slot(poker.S_PHASE) == poker.PH_DONE


def test_zero_leading_word_commit_advances_and_is_fresh_once():
    # (audit) a commitment whose first 4 bytes are zero used to wedge the COMMIT phase forever (word0 sentinel).
    # A dedicated committed-flag now drives both freshness and the advance gate, regardless of the hash bytes.
    d = Poker(); _start(d)
    d.ok(poker.FN_COMMIT, P0, bytes(4) + bytes([0xAB]) * 28)      # P0 commit, leading word == 0
    assert d.slot(poker.S_PHASE) == poker.PH_COMMIT
    assert d.slot(poker.S_COMMITTED0) == 1
    d.reverts(poker.FN_COMMIT, P0, bytes(4) + bytes([0xCD]) * 28)  # re-commit rejected (flag, not word0)
    d.ok(poker.FN_COMMIT, P1, bytes(4) + bytes([0x07]) * 28)      # P1 commit, also leading word == 0
    assert d.slot(poker.S_PHASE) == poker.PH_ACT_P0              # advanced despite both word0 == 0


def test_showdown_no_reveal_splits():
    # (audit) board posted, NEITHER player reveals: after the deadline both stakes are refunded.
    d = Poker(); _start(d)
    _commit(d, 0, card(12, 0), card(12, 1), bytes(range(32)))
    _commit(d, 1, card(0, 0), card(1, 0), bytes(32))
    d.ok(poker.FN_PLAY, P0); d.ok(poker.FN_PLAY, P1)
    _board(d, [card(2, 2), card(5, 3), card(7, 0), card(0, 1), card(3, 2)])
    assert d.slot(poker.S_PHASE) == poker.PH_SHOWDOWN
    d.reverts(poker.FN_TIMEOUT, P0)                       # before the deadline
    d.height = 1_000
    r = d.ok(poker.FN_TIMEOUT, P1)
    assert (_to0(P0), BUYIN) in r.transfers and (_to0(P1), BUYIN) in r.transfers
    assert d.custody == 0 and d.slot(poker.S_PHASE) == poker.PH_DONE


def test_showdown_one_reveal_then_timeout_takes_pot():
    # a single revealer can still unilaterally claim the whole pot if the opponent never reveals.
    d = Poker(); _start(d)
    n0 = bytes(range(32))
    _commit(d, 0, card(12, 0), card(12, 1), n0)
    _commit(d, 1, card(0, 0), card(1, 0), bytes(32))
    d.ok(poker.FN_PLAY, P0); d.ok(poker.FN_PLAY, P1)
    _board(d, [card(2, 2), card(5, 3), card(7, 0), card(0, 1), card(3, 2)])
    d.ok(poker.FN_REVEAL, P0, bytes([card(12, 0), card(12, 1)]) + n0 + bytes([0, 1, 2, 3, 4]))
    d.height = 1_000
    r = d.ok(poker.FN_TIMEOUT, P0)
    assert (_to0(P0), 2 * BUYIN) in r.transfers and d.custody == 0


def test_board_requires_both_to_agree_and_a_player_caller():
    # (audit) the board is uncommitted, so a single party must NOT be able to choose it: caller-gated, and the
    # phase advances only when both players independently post the identical, duplicate-free board.
    d = Poker(); _start(d)
    _commit(d, 0, card(12, 0), card(12, 1), bytes(range(32)))
    _commit(d, 1, card(0, 0), card(1, 0), bytes(32))
    d.ok(poker.FN_PLAY, P0); d.ok(poker.FN_PLAY, P1)
    good = [card(2, 2), card(5, 3), card(7, 0), card(0, 1), card(3, 2)]
    d.reverts(poker.FN_BOARD, 0xCCCC0003, bytes(good))               # a stranger cannot post the board
    d.reverts(poker.FN_BOARD, P0, bytes([card(2, 2)] * 5))           # duplicate cards rejected
    d.ok(poker.FN_BOARD, P0, bytes(good))
    assert d.slot(poker.S_PHASE) == poker.PH_BOARD                   # one post does not advance
    d.reverts(poker.FN_BOARD, P0, bytes(good))                       # same proposer cannot post twice
    other = [card(12, 2), card(11, 3), card(10, 0), card(9, 1), card(8, 2)]
    d.reverts(poker.FN_BOARD, P1, bytes(other))                      # a DIFFERENT board is rejected
    assert d.slot(poker.S_PHASE) == poker.PH_BOARD
    d.ok(poker.FN_BOARD, P1, bytes(good))                            # matching second post advances
    assert d.slot(poker.S_PHASE) == poker.PH_SHOWDOWN


def test_board_disagreement_voids_and_refunds():
    # (audit) if the two players never agree on the board, the deadline voids the hand and refunds both —
    # so a forged self-favoring board can never reach showdown and steal the pot.
    d = Poker(); _start(d)
    _commit(d, 0, card(12, 0), card(12, 1), bytes(range(32)))
    _commit(d, 1, card(0, 0), card(1, 0), bytes(32))
    d.ok(poker.FN_PLAY, P0); d.ok(poker.FN_PLAY, P1)
    d.ok(poker.FN_BOARD, P0, bytes([card(2, 2), card(5, 3), card(7, 0), card(0, 1), card(3, 2)]))
    d.reverts(poker.FN_TIMEOUT, P0)                       # before the deadline
    d.height = 1_000
    r = d.ok(poker.FN_TIMEOUT, P1)
    assert (_to0(P0), BUYIN) in r.transfers and (_to0(P1), BUYIN) in r.transfers
    assert d.custody == 0 and d.slot(poker.S_PHASE) == poker.PH_DONE


def test_non_stake_selectors_reject_attached_value():
    # (audit) only FN_STAKE0/FN_JOIN consume callvalue; value on any other selector must revert (engine refunds
    # it), so attached BIS can never be silently stranded in custody.
    d = Poker(); _start(d)
    for sel in (poker.FN_COMMIT, poker.FN_PLAY, poker.FN_FOLD, poker.FN_BOARD, poker.FN_REVEAL, poker.FN_TIMEOUT):
        d.reverts(sel, P0, bytes(45), value=1)


def test_identity_is_the_authenticated_caller_not_the_payout_address():
    # (audit) player identity binds to the staking CALLER, not the calldata-supplied payout address — closing
    # both the self-join and the payout-collision impersonation footguns.
    d = Poker()
    payout = 0xDEADBEEF
    d.ok(poker.FN_STAKE0, P0, addr28(payout), value=BUYIN)           # P0's payout low-32 != p0_id
    d.reverts(poker.FN_JOIN, P0, addr28(P0), value=BUYIN)            # P0 still cannot occupy both seats
    d.ok(poker.FN_JOIN, P1, addr28(P1), value=BUYIN)
    bad = commit_of(card(12, 0), card(12, 1), bytes(32))
    d.reverts(poker.FN_COMMIT, payout, bad)                          # payout-collision caller cannot act as P0
    d.ok(poker.FN_COMMIT, P0, bad)                                   # the authenticated P0 caller can
    assert d.slot(poker.S_PHASE) == poker.PH_COMMIT


# ----------------------------------------------------------------------------- live regnet (deploy + escrow)
# A full heads-up game needs two funded wallets; the single-wallet fixture can't be both P0 and P1, and the
# SYS_TRANSFER payout path is already exercised live by the AMM/router/vm_value tests (same vm_engine custody).
# So this live test confirms the contract DEPLOYS and the on-chain ESCROW works: P0 stakes -> custody holds
# the buyin and the table advances to WAIT_P1. The full game logic is covered by the offline tests above.
def test_poker_live_deploy_and_escrow(client):
    import json
    import urllib.request
    from time import sleep
    API = "http://127.0.0.1:3031"

    def _get(path):
        with urllib.request.urlopen(API + path, timeout=8) as r:
            return json.load(r)

    info = _get("/api/vm/contracts")
    assert info["enabled"], "vm not enabled"
    for _ in range(25):
        info = _get("/api/vm/contracts")
        if info.get("fork_height") is not None and client.block_height() > info["fork_height"]:
            break
        client.mine(3)

    me = poker.party_id_of(client.address)
    my28 = bytes.fromhex(client.address)
    before = set(_get("/api/vm/contracts")["contracts"])
    client.send(client.address, 1, "vm:deploy", poker.build(me, 1 * UNIT).hex())
    client.mine(2); sleep(0.3)
    addr = (set(_get("/api/vm/contracts")["contracts"]) - before).pop()

    # P0 stakes 1 BIS -> custody holds it, phase -> WAIT_P1
    client.send(__import__("vm_engine").VM_SINK, 1, "vm:call",
                "%s:%s" % (addr, (be4(poker.FN_STAKE0) + my28).hex()))
    client.mine(1); sleep(0.2)
    d = _get("/api/vm/contract/" + addr)
    slots = {int(s["key"]): int(s["value"]) for s in d["storage"]}
    assert int(d["balance"]) == 1 * UNIT, "the stake is escrowed in custody"
    assert slots.get(poker.S_PHASE) == poker.PH_WAIT_P1, "table advanced to WAIT_P1 after P0 staked"
    assert slots.get(poker.addr_word_key(0, 6)) == me, "P0 payout address recorded"


def _mint_rsa_wallet(path, distinct_from_low32):
    """Generate a fresh 4096-bit RSA wallet (the ONLY size keys_load_new accepts — pubkey length in
    {271, 799}; a 2048-bit key yields 450 and fails to load) whose address low-32 differs from
    `distinct_from_low32`, so party_id_of distinguishes the two seats. Returns the address."""
    import essentials
    from Cryptodome.PublicKey import RSA
    while True:
        key = RSA.generate(4096)
        priv = key.exportKey().decode("utf-8")
        pub = key.publickey().exportKey().decode("utf-8")
        address = hashlib.sha224(pub.encode("utf-8")).hexdigest()
        if (int(address, 16) & 0xFFFFFFFF) != (distinct_from_low32 & 0xFFFFFFFF):
            essentials.keys_save(priv, pub, address, path)
            return address


# Full heads-up game on a LIVE regnet node between TWO independently-signing funded wallets — the end-to-end
# proof that escrow + commit/reveal + the both-players-agree board + the showdown payout all work through the
# real node (vm:deploy, vm:call custody on VM_SINK, SYS_TRANSFER payout crediting the winner's real address).
# P0 (the node's mining wallet) holds a pair of Aces; P1 (a minted, P0-funded wallet) holds king-high; on a
# no-help board P0 must win 2*BUYIN and custody must return to 0. Each step is confirmed on-chain between
# mined blocks (regtest_generate drains the mempool into one block, so one tx per block keeps phase order).
def test_poker_live_full_two_player_game(client, tmp_path):
    import json
    import time
    import urllib.request
    import pytest
    import vm_engine
    from _lite_client import LiteClient
    API = "http://127.0.0.1:3031"

    def _get(path, tries=20):
        last = None
        for _ in range(tries):
            try:
                with urllib.request.urlopen(API + path, timeout=8) as r:
                    return json.load(r)
            except Exception as e:
                last = e
                time.sleep(0.5)
        raise last

    def _state(addr):
        d = _get("/api/vm/contract/" + addr)
        return {int(s["key"]): int(s["value"]) for s in d["storage"]}, int(d["balance"])

    def _confirm(c, n=1):
        c.mine(n)
        time.sleep(0.4)                                   # digest runs async after the block commits

    def _phase(addr, want, label, tries=40):
        # Poll by RE-READING (sleep only — the vm:call is already mined by _confirm; do NOT mine more here,
        # which would advance the chain unboundedly and disturb later tests). Under full-suite load the async
        # digest of the vm:call lags the mined block, so the state read right after _confirm is stale; wait
        # for it. For "must not advance" checks the phase is already `want` and this returns immediately; if
        # it wrongly advanced, the poll never matches and the assert still fires.
        slots = bal = None
        for _ in range(tries):
            slots, bal = _state(addr)
            if slots.get(poker.S_PHASE) == want:
                return slots, bal
            time.sleep(0.5)
        assert slots.get(poker.S_PHASE) == want, \
            "%s: phase=%s want=%s (custody=%s)" % (label, slots.get(poker.S_PHASE), want, bal)
        return slots, bal

    p0 = client

    # hf2 must be active before any vm: tx executes
    info = _get("/api/vm/contracts")
    assert info["enabled"], "vm not enabled"
    for _ in range(40):
        info = _get("/api/vm/contracts")
        if info.get("fork_height") is not None and p0.block_height() > info["fork_height"]:
            break
        p0.mine(3)
    else:
        pytest.skip("hf2 fork did not activate on this node")

    # P1 = a fresh wallet (distinct seat id), funded from P0
    p1_path = str(tmp_path / "wallet_p1.der")
    _mint_rsa_wallet(p1_path, poker.party_id_of(p0.address))
    p1 = LiteClient(p1_path, port=3030)
    p0.send(p1.address, 5, "", "")
    p0.mine(2); time.sleep(0.3)
    assert p1.balance() >= 1.0, "P1 funding failed: balance=%s" % p1.balance()

    p0_id, p1_id = poker.party_id_of(p0.address), poker.party_id_of(p1.address)
    assert p0_id != p1_id, "P0/P1 ids collide in low-32 bits"

    # deploy a fresh table with P0 baked in as the only legal seat-0 caller
    before = set(_get("/api/vm/contracts")["contracts"])
    p0.send(p0.address, 1, "vm:deploy", poker.build(p0_id, BUYIN).hex())
    _confirm(p0, 2)
    addr = (set(_get("/api/vm/contracts")["contracts"]) - before).pop()
    slots, bal = _state(addr)
    assert bal == 0 and slots.get(poker.S_PHASE, 0) == poker.PH_OPEN, "fresh table must be empty + PH_OPEN"
    # Tip at deploy time. This game's vm:payout lands at the (later) settle block; in a FULL-SUITE run the
    # shared regnet mining wallet (= P0's real address) has already received vm:payout rows from OTHER VM
    # demos (raffle/AMM/etc.) at LOWER heights — scope the payout check to heights >= here so a foreign
    # payout (e.g. a 3 BIS one) can't be mistaken for poker's pot. (vm:payout rows are negative-height
    # mirrors keyed on the settling block, so compare on abs(block_height).)
    start_height = int(_get("/api/status").get("blocks", 0))

    def vmcall(c, sel_bytes, value=0):
        return c.send(vm_engine.VM_SINK, value, "vm:call", "%s:%s" % (addr, sel_bytes.hex()))

    a0, a1 = bytes.fromhex(p0.address), bytes.fromhex(p1.address)

    # ---- stakes: OPEN -> WAIT_P1 -> COMMIT (1 BIS == BUYIN units into custody each) ----
    vmcall(p0, be4(poker.FN_STAKE0) + a0, value=1); _confirm(p0)
    _, bal = _phase(addr, poker.PH_WAIT_P1, "STAKE0"); assert bal == BUYIN
    vmcall(p1, be4(poker.FN_JOIN) + a1, value=1); _confirm(p1)
    _, bal = _phase(addr, poker.PH_COMMIT, "JOIN"); assert bal == 2 * BUYIN

    # ---- commitments: P0 = AA, P1 = K9 ----
    p0h, p1h = (card(12, 0), card(12, 1)), (card(11, 0), card(8, 1))
    n0, n1 = bytes(range(32)), bytes(reversed(range(32)))
    vmcall(p0, be4(poker.FN_COMMIT) + commit_of(p0h[0], p0h[1], n0)); _confirm(p0)
    _phase(addr, poker.PH_COMMIT, "one commit must not advance")
    vmcall(p1, be4(poker.FN_COMMIT) + commit_of(p1h[0], p1h[1], n1)); _confirm(p1)
    _phase(addr, poker.PH_ACT_P0, "both committed")

    # ---- action: P0 PLAY -> ACT_P1, P1 PLAY -> BOARD ----
    vmcall(p0, be4(poker.FN_PLAY)); _confirm(p0); _phase(addr, poker.PH_ACT_P1, "P0 PLAY")
    vmcall(p1, be4(poker.FN_PLAY)); _confirm(p1); _phase(addr, poker.PH_BOARD, "P1 PLAY")

    # ---- board agreement: BOTH must post the identical 5 distinct cards ----
    board = bytes([card(2, 2), card(3, 3), card(5, 0), card(0, 1), card(4, 2)])   # pairs neither hand
    vmcall(p0, be4(poker.FN_BOARD) + board); _confirm(p0)
    _phase(addr, poker.PH_BOARD, "first board post must not advance")
    vmcall(p1, be4(poker.FN_BOARD) + board); _confirm(p1)
    _phase(addr, poker.PH_SHOWDOWN, "both agreed on the board")

    # ---- reveal + showdown: SHOWDOWN -> DONE, P0 (pair of aces) wins the pot ----
    vmcall(p0, be4(poker.FN_REVEAL) + bytes([p0h[0], p0h[1]]) + n0 + bytes([0, 1, 2, 3, 4])); _confirm(p0)
    slots = None
    for _ in range(40):                                  # poll for P0's reveal flag (digest lag under load; no mining)
        slots, _ = _state(addr)
        if slots.get(poker.S_REVEAL0) == 1:
            break
        time.sleep(0.5)
    assert slots.get(poker.S_REVEAL0) == 1 and slots.get(poker.S_PHASE) == poker.PH_SHOWDOWN, \
        "one reveal sets the flag but must not settle"
    vmcall(p1, be4(poker.FN_REVEAL) + bytes([p1h[0], p1h[1]]) + n1 + bytes([0, 1, 2, 3, 4])); _confirm(p1)
    slots, bal = _phase(addr, poker.PH_DONE, "both revealed -> settle")   # poll for finalization
    assert slots.get(poker.S_DONEFLAG) == 1, "hand finalized"
    assert (slots.get(poker.S_RANK0) >> 20) == 1, "P0 holds a PAIR"
    assert (slots.get(poker.S_RANK1) >> 20) == 0, "P1 holds HIGH CARD"
    assert slots.get(poker.S_RANK0) > slots.get(poker.S_RANK1), "pair of aces beats king-high"
    assert bal == 0, "custody returns to 0 after the pot is paid"

    # the winner was paid the full pot on-chain (vm:payout row to P0's real address; amount in BIS == 2.0).
    # custody already returned to 0 above, so the payout HAS executed; poll (and nudge a block) for its row
    # to surface, because the synthetic vm:payout is indexed asynchronously after digest and — on a busy
    # shared node (full-suite run) where P0 is the mining wallet with a deep history — the address index can
    # lag a confirmation behind the settling block. limit=500 (the max) guards against P0's coinbase volume.
    payouts = []
    deadline = time.time() + 30
    while time.time() < deadline:
        txs = _get("/api/address/%s/transactions?limit=500" % p0.address).get("transactions", [])
        payouts = [t for t in txs if t.get("operation") == "vm:payout"
                   and str(t.get("recipient")) == p0.address
                   and abs(int(t.get("block_height") or 0)) >= start_height]   # this game's settle, not a prior test's
        if payouts:
            break
        time.sleep(0.5)                                  # indexing lag only — payout is already on chain (custody==0)
    assert payouts, "expected a vm:payout crediting P0's real address with the pot"
    assert abs(max(float(t["amount"]) for t in payouts) - (2 * BUYIN) / UNIT) < 1e-9, "P0 paid exactly 2*BUYIN"
