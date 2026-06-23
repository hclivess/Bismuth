"""tests/test_poker_deal.py — Stage 2 (doc/28 §5): the N-party mental-poker DEAL reference
(contracts/poker_deal.py) plus its on-chain anchoring in contracts/poker_table.py (the additive
FN_DECK_DIGEST selector + TAG_DECKH / TAG_CARDMAP_H slots).

Covers, for N = 2..6 (and a spot-check N=9):
  * correctness: all 2N+5 dealt cards distinct + in 0..51; the board reconstructs IDENTICALLY for every
    player; each seat's hole decrypts to the cards it then commits + FN_REVEALs on-chain.
  * secrecy: a coalition of all-but-r cannot recover r's hole; opening one position does not open another;
    the honest client REFUSES to reveal a hole openable without seat r's own key.
  * contract tie-in: FN_DECK_DIGEST anchors each deal-stage hash + the card-map root, read back verbatim;
    the cards a seat will FN_REVEAL match what the deal dealt (commit/reveal round-trips through the VM).

Deterministic RNG (sha256 stream) makes every deal reproducible. Run:
    python3 -m pytest tests/test_poker_deal.py -v
"""
import hashlib
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "contracts"), os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import bismuth_riscv as rv
import poker_deal as d
import poker_table as pt


# --------------------------------------------------------------------------- helpers
def mkrng(seed):
    """A deterministic byte stream so deals are reproducible (NOT for production)."""
    st = [seed if isinstance(seed, bytes) else str(seed).encode()]

    def rng(n):
        out = b""
        while len(out) < n:
            st[0] = hashlib.sha256(st[0]).digest()
            out += st[0]
        return out[:n]

    return rng


def be4(x):
    return (x & 0xFFFFFFFF).to_bytes(4, "big")


def addr28(low32):
    return bytes(24) + (low32 & 0xFFFFFFFF).to_bytes(4, "big")


def run_deal(n, seed):
    deal = d.Deal(n, rng=mkrng(seed))
    deal.run_shuffle()
    deal.run_lock()
    holes = {r: deal.deal_hole(r) for r in range(n)}
    board = deal.reveal_board()
    return deal, holes, board


# --------------------------------------------------------------------------- card<->field map
def test_card_map_roundtrip_and_distinct():
    vals = [d.card_val(c) for c in range(52)]
    assert len(set(vals)) == 52
    for c in range(52):
        assert d.val_card(d.card_val(c)) == c
    with pytest.raises(ValueError):
        d.card_val(52)
    with pytest.raises(ValueError):
        d.val_card(1)          # 1 -> card -1, illegal


# --------------------------------------------------------------------------- correctness, N=2..6 (+9)
@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 9])
def test_deal_correctness(n):
    deal, holes, board = run_deal(n, b"correctness-%d" % n)
    dealt = list(board)
    for r in range(n):
        assert len(holes[r]) == 2
        dealt += holes[r]
    # all 2N+5 dealt cards distinct + in 0..51
    assert len(dealt) == 2 * n + 5
    assert all(0 <= c <= 51 for c in dealt)
    assert len(set(dealt)) == len(dealt)
    # the board reconstructs identically for EVERY player (no one can steer it)
    for r in range(n):
        assert deal.reveal_board_for(r) == board


# --------------------------------------------------------------------------- secrecy
@pytest.mark.parametrize("n", [2, 3, 4, 5, 6])
def test_coalition_cannot_open_victim(n):
    """Every player except r strips their per-position keys on r's hole positions -> the residual is NOT a
    legal card (r's key is still on). victim-key-last holds."""
    deal, holes, _ = run_deal(n, b"coalition-%d" % n)
    for r in range(n):
        residuals = deal.coalition_attempt_hole(r)
        for v in residuals:
            with pytest.raises(ValueError):
                d.val_card(v)   # a coalition without r's key cannot read r's card


@pytest.mark.parametrize("n", [2, 4, 6])
def test_independent_opening(n):
    """Opening one position (publishing all keys for it) must not reveal any OTHER position. We hand a
    coalition every per-position key for position 2r (seat r's first hole) and show position 2r+1 stays
    locked under the keys nobody published."""
    deal, holes, _ = run_deal(n, b"indep-%d" % n)
    r = 1 % n
    p0, p1 = d.hole_positions(r)
    # fully open p0 by stripping EVERY player's key for p0 (this is what a legitimate p0-open does)
    c0 = deal.locked_deck[p0]
    for pl in deal.players:
        c0 = pl.strip_position(p0, c0)
    assert d.val_card(c0) == holes[r][0]        # p0 opened correctly
    # now p1: stripping every player's p0-key (the only keys "published" by the p0 open) does nothing for p1.
    # Concretely, the keys for p0 are independent of p1's keys, so p1 remains a non-card until p1's OWN keys
    # are applied. Demonstrate: applying p0's keys to p1's ciphertext does not yield a legal card.
    c1 = deal.locked_deck[p1]
    for pl in deal.players:
        c1 = d.dec(c1, pl.pos_keys[p0])         # WRONG keys (p0's), as a coalition holding only the p0-open
    with pytest.raises(ValueError):
        d.val_card(c1)


@pytest.mark.parametrize("n", [2, 3, 5])
def test_client_refuses_reveal_without_own_key(n):
    """The honest client's refuse-without-own-key guard fires if a hole position is already openable without
    seat r's key (a protocol/implementation bug or a malicious upstream that pre-stripped r's key)."""
    deal, _, _ = run_deal(n, b"refuse-%d" % n)
    r = 0
    pos = d.hole_positions(r)[0]
    # Simulate the bug: build a ciphertext where r's own key is ALREADY stripped (everyone, including r,
    # stripped) -> it is a legal card, so the guard must refuse.
    cipher = deal.locked_deck[pos]
    for pl in deal.players:
        cipher = pl.strip_position(pos, cipher)     # ALL players, including r -> already plaintext
    with pytest.raises(RuntimeError, match="refuse-without-own-key"):
        deal.players[r].assert_needs_my_key(pos, cipher)
    # the correct flow (others-only) passes the guard
    cipher_ok = deal.locked_deck[pos]
    for pl in deal.players:
        if pl.seat != r:
            cipher_ok = pl.strip_position(pos, cipher_ok)
    deal.players[r].assert_needs_my_key(pos, cipher_ok)   # no raise
    assert deal.players[r].open_my_hole(pos, cipher_ok) == deal.holes[r][0]


# --------------------------------------------------------------------------- contract anchoring tie-in
class TableDriver:
    """Minimal custody-tracking harness around poker_table bytecode (same shape as test_poker_table_vm)."""

    def __init__(self, nseats, sb, bb, max_buyin=10000):
        self.code = pt.build(nseats, sb, bb, max_buyin=max_buyin)
        self.st = {}
        self.custody = 0
        self.height = 100

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
        return r

    def reverts(self, sel, caller, tail=b"", value=0):
        assert not self.call(sel, caller, tail, value).success, "expected revert (sel %d)" % sel

    def slot(self, k):
        return self.st.get(k, 0)


def _read32(drv, key_fn):
    return b"".join(drv.slot(key_fn(w)).to_bytes(4, "big") for w in range(8))


def test_deck_digest_anchors_match_deal():
    """FN_DECK_DIGEST anchors the card-map root + every deal-stage deck hash; read back verbatim."""
    n = 3
    deal, _, _ = run_deal(n, b"anchor")
    drv = TableDriver(n, 1, 2)
    seats = [0xA0, 0xB0, 0xC0]
    for s in range(n):
        drv.ok(pt.FN_SIT, seats[s], be4(s) + addr28(seats[s]), value=100)
    # card-map root
    cm = d.cardmap_digest()
    drv.ok(pt.FN_DECK_DIGEST, seats[0], be4(pt.IDX_CARDMAP) + cm)
    assert _read32(drv, pt.cardmap_h_key) == cm
    # each deal-stage deck hash, posted by rotating seats (any seated party may anchor)
    digs = deal.deal_digests()
    for idx, h in enumerate(digs):
        drv.ok(pt.FN_DECK_DIGEST, seats[idx % n], be4(idx) + h)
    for idx, h in enumerate(digs):
        assert _read32(drv, lambda w, _i=idx: pt.deckh_key(_i, w)) == h


def test_deck_digest_guards():
    """FN_DECK_DIGEST is additive + safe: rejects strangers, attached value, and out-of-range indices."""
    drv = TableDriver(2, 1, 2)
    drv.ok(pt.FN_SIT, 0xA0, be4(0) + addr28(0xA0), value=100)
    drv.ok(pt.FN_SIT, 0xB0, be4(1) + addr28(0xB0), value=100)
    h = hashlib.sha256(b"x").digest()
    drv.ok(pt.FN_DECK_DIGEST, 0xA0, be4(0) + h)               # seated -> ok
    drv.reverts(pt.FN_DECK_DIGEST, 0x9999, be4(0) + h)        # stranger -> revert
    drv.reverts(pt.FN_DECK_DIGEST, 0xA0, be4(0) + h, value=5) # value attached -> revert
    drv.reverts(pt.FN_DECK_DIGEST, 0xA0, be4(pt.DECKH_MAXIDX) + h)  # idx out of range -> revert


@pytest.mark.parametrize("n", [2, 3, 4])
def test_dealt_hole_commits_and_reveals_on_chain(n):
    """End-to-end: the cards a seat will FN_REVEAL match the deal's holes. We build each seat's 5 showdown
    cards as [hole0, hole1, + 3 distinct board cards], commit SHA256(cards|nonce) via FN_COMMIT, run the
    betting to showdown by everyone checking, then FN_REVEAL and confirm the contract accepts the deal's
    cards and ranks them (TAG_RANK set)."""
    deal, holes, board = run_deal(n, b"e2e-%d" % n)
    drv = TableDriver(n, 1, 2, max_buyin=100000)
    seats = [0x100 + 0x10 * s for s in range(n)]
    for s in range(n):
        drv.ok(pt.FN_SIT, seats[s], be4(s) + addr28(seats[s]), value=1000)
    drv.ok(pt.FN_DEAL, 0xDEAD)
    # Each seat's 5 showdown cards: its 2 holes + the first 3 board cards (all globally distinct by the deal).
    showdown = {}
    nonces = {}
    for s in range(n):
        cards = holes[s] + board[:3]
        assert len(set(cards)) == 5, "deal must give each seat 5 distinct cards"
        showdown[s] = cards
        nonces[s] = mkrng(b"nonce-%d-%d" % (n, s))(32)
        drv.ok(pt.FN_COMMIT, seats[s], hashlib.sha256(bytes(cards) + nonces[s]).digest())
    # Walk betting to showdown: everyone CALLs/CHECKs around each street until PH_SHOWDOWN.
    _drive_to_showdown(drv, n, seats)
    assert drv.slot(pt.S_PHASE) == pt.PH_SHOWDOWN
    # Reveal each non-folded seat; the contract checks the commit, distinctness, and ranks via _rank5.
    for s in range(n):
        if drv.slot(pt.state_key(s)) in (pt.ST_ACTIVE, pt.ST_ALLIN):
            drv.ok(pt.FN_REVEAL, seats[s], bytes(showdown[s]) + nonces[s])
            # contract stored exactly the deal's cards
            stored = [drv.slot(pt.card_key(s, k)) for k in range(5)]
            assert stored == showdown[s]
            assert drv.slot(pt.rank_key(s)) > 0       # ranked on-chain
    drv.ok(pt.FN_SETTLE, 0xDEAD)
    assert drv.slot(pt.S_PHASE) == pt.PH_SETTLED


def _drive_to_showdown(drv, n, seats):
    """Everyone just calls/checks; preflop the blinds are already posted, so non-blind seats CALL and the
    blinds CHECK; subsequent streets everyone CHECKs. Loops until PH_SHOWDOWN (bounded)."""
    guard = 0
    while drv.slot(pt.S_PHASE) == pt.PH_BET:
        guard += 1
        assert guard < 200, "betting did not converge"
        toact = drv.slot(pt.S_TOACT)
        # if the seat can check (street_bet == to_call) check, else call
        sb = drv.slot(pt.streetbet_key(toact))
        tc = drv.slot(pt.S_TOCALL)
        if sb == tc:
            drv.ok(pt.FN_CHECK, seats[toact])
        else:
            drv.ok(pt.FN_CALL, seats[toact])
