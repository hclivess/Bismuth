// bismuth-deal-session.js — a DISTRIBUTED orchestration layer over the verified mental-poker primitives in
// bismuth-deal.js (doc/28 §5). It removes the all-knowing coordinator from the Stage-3 poker SPA: each player
// constructs exactly ONE DealSession holding ONLY its own Player (its own shuffle key + its own per-position
// lock keys). NO single party ever holds another seat's keys, and the bus NEVER carries a cleartext card.
//
// The crypto is unchanged — we only SEQUENCE Player.encryptShuffle / Player.lock / Player.stripPosition /
// Player.openMyHole / Player.assertNeedsMyKey over an injected message bus. The bus interface matches
// relay.py's /relay/msg (send) + /relay/inbox (ordered, since-based receive):
//
//   bus.send(msg)            -> push an opaque object onto the per-table ordered list
//   bus.onMessage(cb)        -> deliver every message (in bus order, including our own echoes) to cb(msg)
//
// PROTOCOL (N players, indices 0..N-1; index = compact deal seat, agreed by the caller):
//   * encrypt-shuffle round S_0..S_{N-1}: index 0 starts from the canonical 52-card deck; each index i, once
//     it has the deck after i-1, applies Player.encryptShuffle and broadcasts the resulting deck array
//     (decimal strings, like the demo's deckN messages). After S_{N-1} every player has the shuffled deck.
//   * lock round L_0..L_{N-1}: same baton order; each applies Player.lock (per-position re-key) and broadcasts.
//     After L_{N-1} every player has the fully-locked deck.
//   * deal holes (CHAINED strip, victim-key-last): for each seat r, the N-1 OTHER players strip r's two
//     positions {2r, 2r+1} one after another (baton order over the running cipher). Each stripper applies
//     ONLY Player.stripPosition (NO key leaves the browser; the value stays encrypted under the remaining
//     keys incl. r's) and broadcasts the residual. After all N-1 others have stripped, ONLY r's own key
//     remains; seat r runs Player.openMyHole LOCALLY — and Player.assertNeedsMyKey REFUSES if the residual is
//     already a legal card BEFORE r's key (guards a buggy/malicious early strip). No party except r ever holds
//     all keys for r's positions.
//   * board: every live player strips the 5 board positions in baton order; after all N strips the residual is
//     a legal card. Anyone following the broadcasts reconstructs the identical, un-steerable public board.
//
// On-chain anchoring (FN_DECK_DIGEST) uses the SAME deal_digests / cardmap_digest bytes bismuth-deal.js
// produces. Every player sees the identical broadcast deck for each stage, so every player independently
// computes the identical anchors (no coordinator needed). The reference Deal pushes one stageDecks entry per
// shuffle stage plus one for the locked deck; we hash exactly those arrays in that order.
//
// [SEC-M1] OPTIONAL HARDENING (metadata only): the chained holepart residuals for seat r could additionally be
// e2e-encrypted so the relay cannot see the residual stream. NOT done here and NOT required for secrecy —
// victim-key-last already makes every residual useless to anyone but r (it stays locked under r's own key).
// Pluggable seal/open hooks are accepted so a deployment can layer that on without touching the crypto.

import {
  Player, cardVal, valCard, holePositions, boardPositions, deckPositions,
  deckBytes, cardmapBytes,
} from "./bismuth-deal.js";

export const MSG = {
  SHUFFLE:  "deck.shuffle",  // {stage:i, deck:[dec,...]}            broadcast after index i's encryptShuffle
  LOCK:     "deck.lock",     // {stage:i, deck:[dec,...]}            broadcast after index i's lock
  HOLEPART: "hole.part",     // {forIdx:r, step:k, from:i, pos0,pos1} running residual after i stripped r's hole
  BOARDPART:"board.part",    // {step:k, from:i, vals:[dec x5]}      running residual after i stripped the board
};

const _big = (s) => BigInt(s);
const _dec = (b) => BigInt(b).toString();

// chain order for stripping seat r's positions: every index except r, ascending. r owns the final peel.
function _stripChain(n, r) {
  const out = [];
  for (let i = 0; i < n; i++) if (i !== r) out.push(i);
  return out;
}

// A distributed deal driven by ONE local Player over an injected bus.
//
//   opts:
//     n         (int)                 number of players (compact deal indices 0..n-1)
//     myIdx     (int)                 this client's compact deal index
//     player    (Player)             OPTIONAL pre-made Player(myIdx, n[, rng]); else one is created
//     rng       (fn)                 OPTIONAL rng for the auto-created Player
//     bus       ({send,onMessage})   the message bus (relay-shaped)
//     sha256    (bytes->bytes)       OPTIONAL sync hash for anchor digests (else digests omitted)
//     sealToSeat / openFromSeat      OPTIONAL [SEC-M1] e2e hooks for holepart residuals
//
// Lifecycle: construct, subscribe with start(), then await done() — resolves once this client knows its hole
// AND the board. Callbacks: onHole(cards), onBoard(cards), onStage(kind, stageIdx, deck),
// onAnchor({cardmapDigest, dealDigests}).
export class DealSession {
  constructor(opts) {
    const n = opts.n;
    if (!(n >= 2 && n <= 9)) throw new Error("n must be 2..9");
    this.n = n;
    this.m = deckPositions(n);
    this.myIdx = opts.myIdx;
    if (!(this.myIdx >= 0 && this.myIdx < n)) throw new Error("myIdx out of range");
    this.player = opts.player || new Player(this.myIdx, n, opts.rng || null);
    if (this.player.seat !== this.myIdx) throw new Error("player.seat must equal myIdx");
    this.bus = opts.bus;
    this.sha256 = opts.sha256 || null;
    this.sealToSeat = opts.sealToSeat || null;     // (forIdx, partsObj) -> sealed
    this.openFromSeat = opts.openFromSeat || null; // (sealed) -> partsObj

    this.onHole = opts.onHole || (() => {});
    this.onBoard = opts.onBoard || (() => {});
    this.onStage = opts.onStage || (() => {});
    this.onAnchor = opts.onAnchor || (() => {});

    this.canonicalDeck = [];
    for (let c = 0; c < 52; c++) this.canonicalDeck.push(cardVal(c));

    this.shuffleDecks = new Array(n).fill(null);   // shuffleDecks[i] = deck broadcast after index i shuffled
    this.lockDecks = new Array(n).fill(null);      // lockDecks[i]    = deck broadcast after index i locked
    this.lockedDeck = null;                        // BigInt[]  == lockDecks[n-1]

    // hole chains: holeChain[r] = { step -> {pos0:BigInt, pos1:BigInt} } residual after that step's stripper
    this.holeChain = {};
    for (let r = 0; r < n; r++) this.holeChain[r] = {};
    // board chain: boardChain[step] = BigInt[5] residual after that step's stripper
    this.boardChain = {};

    this.hole = null;
    this.board = null;

    this._postedShuffle = false;
    this._postedLock = false;
    this._postedHoleStep = {};     // "r:step" -> true
    this._postedBoardStep = {};    // step -> true
    this._anchored = false;

    this._started = false;
    this._resolveDone = null;
    this._donePromise = new Promise((res) => { this._resolveDone = res; });
  }

  // -------- public API ----------------------------------------------------
  start() {
    if (this._started) return;
    this._started = true;
    this.bus.onMessage((msg) => this._onMessage(msg));
    this._drive();   // kick off (index 0's shuffle seed)
  }

  done() { return this._donePromise; }

  // -------- bus ----------------------------------------------------------
  _send(type, data) { this.bus.send({ type, idx: this.myIdx, data }); }

  _onMessage(msg) {
    if (!msg || typeof msg !== "object") return;
    const t = msg.type;
    const d = msg.data || {};
    if (t === MSG.SHUFFLE) {
      const i = d.stage;
      if (this.shuffleDecks[i] == null) this.shuffleDecks[i] = d.deck.map(_big);
    } else if (t === MSG.LOCK) {
      const i = d.stage;
      if (this.lockDecks[i] == null) this.lockDecks[i] = d.deck.map(_big);
    } else if (t === MSG.HOLEPART) {
      this._recordHolePart(msg);
    } else if (t === MSG.BOARDPART) {
      const k = d.step;
      // Integrity: the board is stripped in a sequential baton, so step k must come from player k, and the
      // envelope idx must match the claimed `from`. Reject forged/mismatched steps so a malicious live player
      // can't inject a bogus residual and wedge the board (secrecy is unaffected — all live keys are needed).
      if (Number.isInteger(k) && k >= 0 && k < this.n && msg.idx === k && d.from === msg.idx) {
        if (this.boardChain[k] == null) this.boardChain[k] = d.vals.map(_big);
      }
    }
    this._drive();
  }

  _recordHolePart(msg) {
    const d = msg.data || {};
    const r = d.forIdx, k = d.step;
    // Integrity: step k of seat r's hole must come from its EXPECTED stripper (the k-th non-r player), and the
    // envelope idx must match the claimed `from`. Without this a malicious live player could forge another
    // stripper's step and wedge the victim's reveal (assertNeedsMyKey throw / never-legal-card). Secrecy is
    // unaffected — victim-key-last seals every residual regardless — this only stops the DoS. [review LOW #1]
    if (!Number.isInteger(r) || r < 0 || r >= this.n) return;
    const chain = _stripChain(this.n, r);
    if (!Number.isInteger(k) || k < 0 || k >= chain.length) return;
    if (msg.idx !== chain[k] || d.from !== msg.idx) return;
    let parts;
    if (d.sealed !== undefined) {
      // [SEC-M1] only the recipient r can open a sealed residual; others store it opaque (still harmless).
      if (r === this.myIdx && this.openFromSeat) parts = this.openFromSeat(d.sealed);
      else return;
    } else {
      const [a, b] = holePositions(r);
      parts = { [a]: d.pos0, [b]: d.pos1 };
    }
    if (this.holeChain[r][k] == null) {
      const [a, b] = holePositions(r);
      this.holeChain[r][k] = { [a]: _big(parts[a]), [b]: _big(parts[b]) };
    }
  }

  // -------- the driver: idempotent; re-run on every state change ----------
  _drive() {
    if (this.board && this.hole) return;   // fully done
    this._maybeShuffle();
    this._maybeLock();
    this._onceLocked();
    this._maybeOpenMyHole();
    this._maybeBoard();
    if (this.hole && this.board && this._resolveDone) {
      const done = this._resolveDone; this._resolveDone = null;
      done({ hole: this.hole.slice(), board: this.board.slice() });
    }
  }

  // encrypt-shuffle baton
  _maybeShuffle() {
    if (this._postedShuffle) return;
    let input;
    if (this.myIdx === 0) input = this.canonicalDeck;
    else { input = this.shuffleDecks[this.myIdx - 1]; if (!input) return; }
    const out = this.player.encryptShuffle(input);
    this.shuffleDecks[this.myIdx] = out.slice();
    this._postedShuffle = true;
    this._send(MSG.SHUFFLE, { stage: this.myIdx, deck: out.map(_dec) });
    this.onStage("shuffle", this.myIdx, out);
  }

  // lock baton — needs the fully-shuffled deck first
  _maybeLock() {
    if (this._postedLock) return;
    const shuffled = this.shuffleDecks[this.n - 1];
    if (!shuffled) return;
    let input;
    if (this.myIdx === 0) input = shuffled;
    else { input = this.lockDecks[this.myIdx - 1]; if (!input) return; }
    const out = this.player.lock(input);
    this.lockDecks[this.myIdx] = out.slice();
    this._postedLock = true;
    this._send(MSG.LOCK, { stage: this.myIdx, deck: out.map(_dec) });
    this.onStage("lock", this.myIdx, out);
  }

  _onceLocked() {
    const locked = this.lockDecks[this.n - 1];
    if (!locked) return;
    if (!this.lockedDeck) {
      this.lockedDeck = locked.slice();
      this._anchorIfReady();
    }
    // contribute our strip to every OTHER seat's hole chain when it's our turn in that chain
    for (let r = 0; r < this.n; r++) {
      if (r === this.myIdx) continue;
      this._maybeHoleStep(r);
    }
    // contribute our strip to the board chain when it's our turn
    this._maybeBoardStep();
  }

  // our position in seat r's chain; strip the running residual and broadcast
  _maybeHoleStep(r) {
    const chain = _stripChain(this.n, r);     // ascending others
    const myStep = chain.indexOf(this.myIdx);
    if (myStep < 0) return;
    const key = r + ":" + myStep;
    if (this._postedHoleStep[key]) return;
    const [a, b] = holePositions(r);
    let prev0, prev1;
    if (myStep === 0) { prev0 = this.lockedDeck[a]; prev1 = this.lockedDeck[b]; }
    else {
      const pr = this.holeChain[r][myStep - 1];
      if (!pr) return;                         // upstream stripper hasn't posted yet
      prev0 = pr[a]; prev1 = pr[b];
    }
    const res0 = this.player.stripPosition(a, prev0);
    const res1 = this.player.stripPosition(b, prev1);
    const residual = { [a]: res0, [b]: res1 };
    this.holeChain[r][myStep] = residual;      // record our own contribution locally too
    this._postedHoleStep[key] = true;
    let payload = { forIdx: r, step: myStep, from: this.myIdx };
    if (this.sealToSeat) {
      payload.sealed = this.sealToSeat(r, { [a]: _dec(res0), [b]: _dec(res1) });
    } else {
      payload.pos0 = _dec(res0); payload.pos1 = _dec(res1);
    }
    this._send(MSG.HOLEPART, payload);
  }

  _maybeBoardStep() {
    if (this._postedBoardStep[this.myIdx]) return;
    const positions = boardPositions(this.n);
    let prev;
    if (this.myIdx === 0) prev = positions.map((p) => this.lockedDeck[p]);
    else {
      const pr = this.boardChain[this.myIdx - 1];
      if (!pr) return;
      prev = pr;
    }
    const residual = positions.map((p, j) => this.player.stripPosition(p, prev[j]));
    this.boardChain[this.myIdx] = residual;
    this._postedBoardStep[this.myIdx] = true;
    this._send(MSG.BOARDPART, { step: this.myIdx, from: this.myIdx, vals: residual.map(_dec) });
  }

  // open OUR hole once all N-1 others have stripped (victim-key-last + refuse-without-own-key guard)
  _maybeOpenMyHole() {
    if (this.hole) return;
    if (!this.lockedDeck) return;
    const r = this.myIdx;
    const chain = _stripChain(this.n, r);
    const lastStep = chain.length - 1;         // last OTHER stripper's step index
    const [a, b] = holePositions(r);
    let afterOthers;
    if (chain.length === 0) {
      afterOthers = { [a]: this.lockedDeck[a], [b]: this.lockedDeck[b] }; // n==1 (not reachable, n>=2)
    } else {
      const tail = this.holeChain[r][lastStep];
      if (!tail) return;                        // not all others have stripped yet
      afterOthers = tail;
    }
    // victim-key-last: each card must be ILLEGAL before our key, then legal after our strip.
    const cards = [];
    for (const pos of [a, b]) {
      cards.push(this.player.openMyHole(pos, afterOthers[pos]));
    }
    this.hole = cards;
    this.onHole(cards.slice());
  }

  _maybeBoard() {
    if (this.board) return;
    if (!this.lockedDeck) return;
    const last = this.n - 1;
    const tail = this.boardChain[last];
    if (!tail) return;                          // not all players stripped the board yet
    // after all N strips, each residual must be a legal card
    const board = tail.map((v) => valCard(v));
    this.board = board;
    this.onBoard(board.slice());
  }

  // -------- on-chain anchoring (same bytes as bismuth-deal.js) -------------
  // sha256 may be SYNC (returns bytes; node cross-check) or ASYNC (returns a Promise<bytes>; WebCrypto in the
  // browser). Anchoring is OFF the protocol critical path, so we resolve any Promises before onAnchor — the
  // deal completes regardless. Each player sees the identical stage decks, so all anchors agree.
  _anchorIfReady() {
    if (this._anchored || !this.sha256) return;
    // reference Deal.stageDecks order: shuffleDecks[0..n-1] then the locked deck.
    if (this.shuffleDecks.some((d) => d == null) || !this.lockedDeck) return;
    this._anchored = true;
    const stageDecks = [];
    for (let i = 0; i < this.n; i++) stageDecks.push(this.shuffleDecks[i]);
    stageDecks.push(this.lockedDeck);
    const rawDigests = stageDecks.map((d) => this.sha256(deckBytes(d)));
    const rawCm = this.sha256(cardmapBytes());
    const isPromise = rawCm && typeof rawCm.then === "function";
    if (!isPromise) {
      this.onAnchor({ cardmapDigest: rawCm, dealDigests: rawDigests, stageDecks });
      return;
    }
    Promise.all([Promise.resolve(rawCm), ...rawDigests.map((p) => Promise.resolve(p))])
      .then((all) => {
        const cardmapDigest = all[0];
        const dealDigests = all.slice(1);
        this.onAnchor({ cardmapDigest, dealDigests, stageDecks });
      })
      .catch(() => { /* anchoring is best-effort; never blocks the deal */ });
  }
}
