"""
poker_deal.py — Stage 2 (doc/28 §5): a PURE-PYTHON reference implementation of the N-party decentralized
mental-poker DEAL for Bismuth Hold'em. It generalizes the heads-up SRA commutative-cipher shuffle (the one in
web/poker/index.html, lines ~83-170) to N players with NO trusted dealer, and adds the load-bearing
Barnett-Smart per-position LOCKING round so any one card can be opened independently without leaking the
others. It simulates the N parties IN-PROCESS (no network / no relay needed), but every step is expressed as a
discrete, message-oriented operation so a later JS client carrying these messages over web/poker/relay.py
(the key-blind message bus) mirrors this exactly.

THE DECK (doc/28 §5)
  2N+5 live positions. Holes: seat s owns positions {2s, 2s+1}. Board: positions 2N .. 2N+4.

THE PROTOCOL
  (1) ENCRYPT-SHUFFLE round S_0 -> ... -> S_{N-1}: each player, in turn, shuffles the WHOLE deck and SRA-
      encrypts every card under their per-hand key k_p. SRA is commutative (enc_a(enc_b(v)) == enc_b(enc_a(v)))
      so after the round the deck is encrypted under ALL N keys and no single player knows the permutation.
  (2) LOCKING / per-position RE-KEYING (Barnett-Smart): each player REPLACES its single shuffle key k_p with
      an INDIVIDUAL per-position key l_{p,i} for every position i. After this round position i is encrypted
      under {l_{0,i}, .., l_{N-1,i}}. Opening position i now needs only those N keys — and leaks nothing about
      any other position j (whose keys l_{*,j} are unrelated). This is THE secrecy step; it is not optional.
  (3) DEAL A HOLE to seat r ({2r, 2r+1}): EVERY OTHER player p posts the partial decryption (strips its
      per-position key l_{p,i}) for ONLY those two positions; seat r strips ITS OWN per-position key LAST and
      privately. No coalition excluding r ever holds all N keys for r's positions -> r's hole stays secret
      (victim-key-last).
  (4) BOARD: for each street's positions, ALL live players post partial decryptions; the product of strips
      yields the plaintext board card, identical for everyone (no one can steer the board).

ANCHORING (on-chain, additive to poker_table.py)
  card-map root  -> deck-stage hashes        -> per-seat showdown commitments -> board derived -> FN_REVEAL.
  TAG_CARDMAP_H  -> TAG_DECKH[stage] (via the -> TAG_COMMIT[s] (SHA256 of the
                    new FN_DECK_DIGEST(idx,H32))   5 showdown cards)
  `deal_digests()` / `cardmap_digest()` produce the exact 32-byte hashes a player posts via FN_DECK_DIGEST.
  The chain pins each deck HASH; it does NOT verify an honest shuffle (that needs ZK proofs, out of scope).

HONEST LIMITS [SEC-H1] (documented, NOT cryptographically enforced here)
  * Secrecy of a hole rests on (a) the enforced locking round, (b) >= 1 honest shuffler, and (c) victim-key-
    last. Soft collusion (players voluntarily showing each other their own cards) is irreducible.
  * The DEMO uses the secp256k1 field prime P with the trivial card map cardVal(c)=c+2 (byte-identical to the
    heads-up web demo). Before real money this MUST move to a large SAFE prime + a quadratic-residue (QR) card
    encoding so the Legendre symbol of a ciphertext leaks nothing. We NOTE this; we do not implement QR here.
  * Routing/timing metadata over the bus is a documented side channel (mitigated by e2e-encrypting holeparts).

The honest client REFUSES to reveal any hole that is openable without its own key: `Player.open_my_hole`
asserts via `assert_needs_my_key` that seat r's per-position key is still present on both of r's positions
before r strips it, so a buggy/malicious flow that already stripped r's key cannot trick r into "revealing"
a value a coalition could have computed without it.
"""

import hashlib
import os

# secp256k1 field prime = 2^256 - 2^32 - 977 (the well-known curve prime, 64 hex digits / 256 bits). This is
# the SAME prime the heads-up web demo intends (web/poker/index.html line ~86); the JS client must use the
# exact 64-hex-digit constant 0xFFFFFFFF...FFFFFFFEFFFFFC2F (a short/mistyped literal is NOT prime and breaks
# the SRA roundtrip — we write it as an expression so it is unambiguous and verifiably prime).
# DEMO-GRADE ONLY: see HONEST LIMITS [SEC-H1] above (safe prime + QR encoding required before real money).
P = 2 ** 256 - 2 ** 32 - 977
PM1 = P - 1
assert pow(2, P - 1, P) == 1, "P must be prime (Fermat check) for SRA to be invertible"


# --------------------------------------------------------------------------- card <-> field map
def card_val(c):
    """Map a card 0..51 to a distinct field element 2..53 (byte-identical to the web demo's cardVal)."""
    if not (0 <= c <= 51):
        raise ValueError("card out of range 0..51")
    return c + 2


def val_card(x):
    """Inverse of card_val: field element -> card 0..51 (raises if not a legal card value)."""
    c = int(x) - 2
    if not (0 <= c <= 51):
        raise ValueError("decrypted value %d is not a legal card element" % int(x))
    return c


def cardmap_bytes():
    """Canonical serialization of the agreed card->value map (what every player hashes for the chain root).
    A simple, deterministic encoding of the 52 (card, value) pairs; the JS client mirrors it byte-for-byte."""
    out = bytearray()
    out += b"SRA-CARDMAP-v1|P="
    out += hex(P).encode()
    out += b"|"
    for c in range(52):
        out += b"%d:%d," % (c, card_val(c))
    return bytes(out)


def cardmap_digest():
    """SHA256 of the canonical card-map serialization — anchored on-chain via FN_DECK_DIGEST(IDX_CARDMAP)."""
    return hashlib.sha256(cardmap_bytes()).digest()


# --------------------------------------------------------------------------- SRA key arithmetic
def _egcd(a, b):
    if b == 0:
        return (a, 1, 0)
    g, x, y = _egcd(b, a % b)
    return (g, y, x - (a // b) * y)


def _modinv(a, m):
    g, x, _ = _egcd(a % m, m)
    if g != 1:
        raise ValueError("no modular inverse")
    return x % m


def gen_key(rng=None):
    """Generate an SRA key (k, kinv) with k in [2,P-2], gcd(k,P-1)==1, k*kinv == 1 (mod P-1). The key never
    leaves a player (the bus is key-blind). `rng` is an optional callable(nbytes)->bytes for deterministic
    tests; defaults to os.urandom."""
    rnd = rng or os.urandom
    while True:
        k = int.from_bytes(rnd(32), "big") % (PM1 - 3) + 2
        if _egcd(k, PM1)[0] == 1:
            return (k, _modinv(k, PM1))


def enc(v, key):
    """SRA encrypt: v^k mod P (commutative composition: enc(enc(v,a),b) == enc(enc(v,b),a))."""
    return pow(int(v) % P, key[0], P)


def dec(v, key):
    """SRA decrypt / strip a key: v^kinv mod P."""
    return pow(int(v) % P, key[1], P)


# --------------------------------------------------------------------------- the deal
def deck_positions(n):
    """Total live positions for an N-player deal: 2N holes + 5 board."""
    return 2 * n + 5


def hole_positions(seat):
    """The two deck positions that become seat `seat`'s hole cards."""
    return (2 * seat, 2 * seat + 1)


def board_positions(n):
    """The five board deck positions for an N-player deal."""
    return list(range(2 * n, 2 * n + 5))


class Player:
    """One party in the deal. Holds its secret SRA shuffle key and, after locking, its per-position keys.
    Nothing here is ever transmitted; only the deck arrays and per-position partial decryptions (which embed
    no key) cross the bus."""

    def __init__(self, seat, n, rng=None):
        self.seat = seat
        self.n = n
        self.rng = rng
        self.shuffle_key = gen_key(rng)            # k_p for the encrypt-shuffle round
        self.pos_keys = None                       # list[(l,linv)] per position, set at lock()
        self._perm = None                          # this player's shuffle permutation (private)

    # ---- (1) encrypt-shuffle ----
    def encrypt_shuffle(self, deck):
        """Shuffle the whole deck and SRA-encrypt every entry under k_p. Input/output are field-element lists
        (messages on the bus carry these as decimal strings, exactly like the web demo's deck1/deck2)."""
        idx = list(range(len(deck)))
        _shuffle(idx, self.rng)
        self._perm = idx
        return [enc(deck[i], self.shuffle_key) for i in idx]

    # ---- (2) Barnett-Smart per-position locking ----
    def lock(self, deck):
        """Re-key: strip the single shuffle key k_p and re-encrypt EACH position under a fresh INDIVIDUAL key
        l_{p,i}. After this the player no longer holds k_p; opening position i needs only l_{p,i} (and the
        other players' l_{*,i}), so opening i cannot help open j. Returns the new (still fully-locked) deck."""
        m = len(deck)
        self.pos_keys = [gen_key(self.rng) for _ in range(m)]
        out = []
        for i in range(m):
            stripped = dec(deck[i], self.shuffle_key)      # remove k_p from position i
            out.append(enc(stripped, self.pos_keys[i]))    # apply the per-position lock l_{p,i}
        self.shuffle_key = None                            # k_p is gone; only per-position keys remain
        return out

    # ---- (3)/(4) partial decryption of a single position ----
    def strip_position(self, position, cipher):
        """Post the partial decryption of ONE position: strip this player's per-position key l_{p,position}.
        This is the only secret-bearing operation a player performs for someone else's hole / the board, and
        it reveals nothing about any other position (different keys). Returns the partially-stripped cipher."""
        if self.pos_keys is None:
            raise RuntimeError("must lock() before stripping positions")
        return dec(cipher, self.pos_keys[position])

    # ---- the refuse-without-own-key guard ----
    def assert_needs_my_key(self, position, cipher_after_others):
        """HONEST-CLIENT GUARD: assert that `cipher_after_others` (everyone else's strips already applied to
        one of seat r's hole positions) is NOT already a legal plaintext card — i.e. it still needs OUR key.
        If it were already openable without our key, a coalition could read our hole, so we REFUSE to proceed.
        Raises RuntimeError on a detected secrecy violation."""
        try:
            c = val_card(cipher_after_others)
        except ValueError:
            return  # not a legal card value -> still locked under our key, as required
        raise RuntimeError(
            "refuse-without-own-key: position %d already decrypts to card %d WITHOUT my key l_{%d,%d}; a "
            "coalition could open my hole -> aborting reveal [SEC-H1]" % (position, c, self.seat, position))

    def open_my_hole(self, position, cipher_after_others):
        """Seat r strips its OWN per-position key LAST and privately, AFTER every other player has stripped
        theirs (victim-key-last). Runs the refuse-without-own-key guard first."""
        self.assert_needs_my_key(position, cipher_after_others)
        return val_card(self.strip_position(position, cipher_after_others))


def _shuffle(arr, rng=None):
    """In-place Fisher-Yates using `rng` (callable(nbytes)->bytes) or os.urandom; matches the demo's intent."""
    rnd = rng or os.urandom
    for i in range(len(arr) - 1, 0, -1):
        j = int.from_bytes(rnd(4), "big") % (i + 1)
        arr[i], arr[j] = arr[j], arr[i]


class Deal:
    """Drives a full N-party deal in-process. The sequence of public deck arrays it produces is exactly what
    the bus would carry; `players[r].shuffle_key/pos_keys` are the per-player secrets that never transmit."""

    def __init__(self, n, rng=None):
        if not (2 <= n <= 9):
            raise ValueError("n must be 2..9")
        self.n = n
        self.m = deck_positions(n)
        self.players = [Player(s, n, rng) for s in range(n)]
        self.stage_decks = []     # public deck array after each shuffle stage (for FN_DECK_DIGEST anchoring)
        self.locked_deck = None   # deck after the locking round
        self.holes = {}           # seat -> [card,card] (each known only to its owner in real play)
        self.board = None         # [card x5]

    # ---- (1) encrypt-shuffle round S_0 -> ... -> S_{N-1} ----
    def run_shuffle(self):
        # Start from the FULL 52-card deck (all distinct, all in 0..51); each player shuffles+encrypts the
        # whole 52. After the round the deck is encrypted under all N keys; the live deal uses positions
        # 0..m-1 (the rest are the undealt remainder of a real 52-card pack). The shuffle randomizes which
        # physical card lands in each live position, so the 2N+5 dealt cards are a uniform draw.
        deck = [card_val(c) for c in range(52)]
        for p in self.players:
            deck = p.encrypt_shuffle(deck)
            self.stage_decks.append(list(deck))
        self.shuffled_deck = deck
        return deck

    # ---- (2) locking round ----
    def run_lock(self):
        deck = self.shuffled_deck
        for p in self.players:
            deck = p.lock(deck)
        self.locked_deck = deck
        self.stage_decks.append(list(deck))   # the locked deck is the final anchored stage
        return deck

    # ---- (3) deal seat r's hole (victim-key-last) ----
    def deal_hole(self, r):
        """Returns seat r's two hole cards. Every player != r strips first; seat r strips last via the guarded
        open_my_hole. A coalition of all-but-r can run the same strips but is left holding a value still locked
        under r's key (see test_secrecy)."""
        cards = []
        for pos in hole_positions(r):
            cipher = self.locked_deck[pos]
            for p in self.players:
                if p.seat != r:
                    cipher = p.strip_position(pos, cipher)     # others strip (any order)
            card = self.players[r].open_my_hole(pos, cipher)   # victim strips LAST, with the guard
            cards.append(card)
        self.holes[r] = cards
        return cards

    def coalition_attempt_hole(self, r):
        """ADVERSARIAL: every player EXCEPT r strips their per-position key for r's positions. Returns the
        residual ciphers — which are NOT legal cards because r's key is still on. Used by the secrecy test."""
        residuals = []
        for pos in hole_positions(r):
            cipher = self.locked_deck[pos]
            for p in self.players:
                if p.seat != r:
                    cipher = p.strip_position(pos, cipher)
            residuals.append(cipher)
        return residuals

    # ---- (4) board: all live players partial-decrypt ----
    def reveal_board(self, live_seats=None):
        """Combine all live players' strips on each board position -> the public board (same for everyone).
        `live_seats` defaults to all seats; only listed seats contribute (folded/away players' keys are still
        needed in real play, so in practice all who locked must strip — this models the cooperative case)."""
        seats = list(range(self.n)) if live_seats is None else list(live_seats)
        board = []
        for pos in board_positions(self.n):
            cipher = self.locked_deck[pos]
            for s in seats:
                cipher = self.players[s].strip_position(pos, cipher)
            board.append(val_card(cipher))
        self.board = board
        return board

    def reveal_board_for(self, observer_seat):
        """Independent reconstruction by one observer (collect everyone's strips, combine locally). Must equal
        reveal_board() for every observer — no one can steer the board."""
        board = []
        for pos in board_positions(self.n):
            cipher = self.locked_deck[pos]
            for p in self.players:
                cipher = p.strip_position(pos, cipher)
            board.append(val_card(cipher))
        return board

    # ---- anchoring digests (feed FN_DECK_DIGEST) ----
    def deal_digests(self):
        """SHA256 of each anchored deck stage (shuffle stages 0..N-1 then the locked deck). Each is posted via
        FN_DECK_DIGEST(idx, H32). Returns list[bytes32]."""
        return [hashlib.sha256(_deck_bytes(d)).digest() for d in self.stage_decks]


def _deck_bytes(deck):
    """Canonical big-endian serialization of a deck (list of field elements) for hashing — JS mirrors it."""
    out = bytearray()
    for v in deck:
        b = int(v).to_bytes((max(int(v), 1).bit_length() + 7) // 8 or 1, "big")
        out += len(b).to_bytes(2, "big") + b
    return bytes(out)
