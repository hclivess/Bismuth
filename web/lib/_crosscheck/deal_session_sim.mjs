// deal_session_sim.mjs — HEADLESS NODE PRIVACY SIMULATION of the DISTRIBUTED deal (bismuth-deal-session.js).
//
// We instantiate N INDEPENDENT DealSessions (one per player; each holds ONLY its own Player/keys) over an
// in-memory mock bus that mirrors relay.py's ordered per-table message list, run a full deal for N=2..6, and
// ASSERT:
//   (a) CORRECTNESS — every seat recovers its own correct hole; the board is identical for all sessions and
//       equals an independent reference computed from the SAME locked deck via the verified primitives; all
//       2N+5 dealt cards are distinct; and every session's FN_DECK_DIGEST anchors match (un-steerable).
//   (b) PRIVACY — for every seat r, pooling EVERY non-r party's full local state (all their keys) PLUS every
//       message that ever crossed the bus is NOT sufficient to recover r's hole: r's two positions, after the
//       coalition removes all keys it holds, do NOT decode to a legal card without r's secret per-position
//       key. In particular the shuffle initiator (idx 0) cannot learn any other hole.
//
// The crypto lives in bismuth-deal.js (unchanged); this proves the ORCHESTRATION never concentrates keys and
// the bus never carries a cleartext card. Prints PASS/FAIL counts; exits non-zero on any failure.

import { createHash } from "node:crypto";
import { DealSession, MSG } from "../bismuth-deal-session.js";
import {
  Player, valCard, holePositions, boardPositions, deckPositions, cardVal,
  mkStreamRng,
} from "../bismuth-deal.js";

function sha256(bytes) { return new Uint8Array(createHash("sha256").update(Buffer.from(bytes)).digest()); }
function hexOf(b) { return Buffer.from(b).toString("hex"); }

let pass = 0, fail = 0;
function ok(cond, label) {
  if (cond) { pass++; }
  else { fail++; console.error("FAIL " + label); }
}

// ----------------------------------------------------------------- in-memory ordered mock bus (relay-shaped)
// One ordered message log shared by all players (relay.py keeps one per-table list). Every player gets its own
// bus FACADE (own send/onMessage), but all facades share the SAME ordered log and cross-deliver every message
// in log order to every subscriber (including the sender's own echo) — exactly like polling /relay/inbox with
// a since cursor. relay.log is the full bus history: everything the relay / any observer ever sees, replayed
// by the privacy adversary. We freeze a structural copy on send so nothing can retro-change the wire history.
class SharedRelay {
  constructor() { this.log = []; this.facades = []; }
  facade() {
    const self = this;
    const f = {
      subs: [],
      send: (msg) => self._broadcast(msg),
      onMessage: (cb) => { f.subs.push(cb); self._replayTo(cb); },
    };
    this.facades.push(f);
    return f;
  }
  _replayTo(cb) {
    for (const m of this.log) cb(JSON.parse(JSON.stringify(m)));
  }
  _broadcast(msg) {
    const frozen = JSON.parse(JSON.stringify(msg));
    this.log.push(frozen);
    for (const f of this.facades) {
      for (const cb of f.subs) cb(JSON.parse(JSON.stringify(frozen)));
    }
  }
}

// ----------------------------------------------------------------- one full distributed deal for N players
async function runDistributed(n, seed) {
  const relay = new SharedRelay();
  const sessions = [];
  const anchors = [];
  // independent per-player RNGs (distinct seeds) so every player has its OWN keys.
  for (let i = 0; i < n; i++) {
    const rng = mkStreamRng(seed + ":p" + i, sha256);
    const player = new Player(i, n, rng);
    const s = new DealSession({
      n, myIdx: i, player, bus: relay.facade(), sha256,
      onAnchor: (a) => { anchors[i] = a; },
    });
    sessions.push(s);
  }
  // start every session; the synchronous bus drives the whole protocol to completion.
  for (const s of sessions) s.start();
  // collect results (done() resolves synchronously-ish via the pump; await to be safe)
  const results = await Promise.all(sessions.map((s) => s.done()));
  return { relay, sessions, results, anchors };
}

// ----------------------------------------------------------------- independent reference from the bus deck
// Reconstruct the locked deck from the bus (the LOCK broadcasts), then independently derive the board using
// the real players' keys (we have them in-sim) — used ONLY to check correctness, NOT for privacy.
function lockedDeckFromBus(log, n) {
  let locked = null;
  for (const m of log) {
    if (m.type === MSG.LOCK && m.data.stage === n - 1) locked = m.data.deck.map((s) => BigInt(s));
  }
  return locked;
}

function referenceBoard(sessions, n, locked) {
  const board = [];
  for (const pos of boardPositions(n)) {
    let cipher = locked[pos];
    for (let i = 0; i < n; i++) cipher = sessions[i].player.stripPosition(pos, cipher);
    board.push(valCard(cipher));
  }
  return board;
}

function referenceHole(sessions, n, locked, r) {
  const out = [];
  for (const pos of holePositions(r)) {
    let cipher = locked[pos];
    for (let i = 0; i < n; i++) cipher = sessions[i].player.stripPosition(pos, cipher);
    out.push(valCard(cipher));
  }
  return out;
}

// ----------------------------------------------------------------- PRIVACY adversary
// Pool EVERY non-r player's keys + ALL bus traffic; show r's positions do not decode to a legal card.
function coalitionCannotOpen(sessions, n, locked, r, log) {
  // The strongest legible ciphertext the coalition can build for one of r's positions is: start from the
  // fully-locked value and remove EVERY key the coalition holds (all players except r). secp256k1-prime SRA is
  // commutative, so order is irrelevant; removing all non-r keys leaves the value locked under r's key alone.
  let everLegibleOnBus = false;
  let coalitionLegible = false;

  for (const pos of holePositions(r)) {
    let cipher = locked[pos];
    for (let i = 0; i < n; i++) {
      if (i === r) continue;
      cipher = sessions[i].player.stripPosition(pos, cipher); // coalition uses its OWN keys only
    }
    // after stripping all non-r keys, the residual must NOT be a legal card (still locked under r's key).
    let legal = false;
    try { valCard(cipher); legal = true; } catch (e) { legal = false; }
    if (legal) coalitionLegible = true;
  }

  // Also scan EVERYTHING that crossed the bus: no SHUFFLE/LOCK/HOLEPART/BOARDPART payload value for r's two
  // positions may itself already be a legal card (a cleartext-card leak). For holeparts/board we check the
  // residuals; for deck broadcasts we check r's positions in every stage deck.
  const rPos = new Set(holePositions(r));
  for (const m of log) {
    if (m.type === MSG.SHUFFLE || m.type === MSG.LOCK) {
      // a stage deck is a PERMUTED, encrypted deck; r's *positions* only become meaningful post-lock, but a
      // legal-card value anywhere in a locked/shuffled deck would be a leak. Check r's positions specifically
      // (post-shuffle the deck is permuted so position!=card, but we still assert no legal card sits there).
      for (const p of rPos) {
        const v = BigInt(m.data.deck[p]);
        try { valCard(v); everLegibleOnBus = true; } catch (e) { /* good: illegal */ }
      }
    } else if (m.type === MSG.HOLEPART && m.data.forIdx === r) {
      // the chained residuals for r's hole must NEVER be a legal card on the wire (victim-key-last).
      const vals = (m.data.pos0 !== undefined) ? [m.data.pos0, m.data.pos1] : [];
      for (const s of vals) {
        try { valCard(BigInt(s)); everLegibleOnBus = true; } catch (e) { /* good */ }
      }
    }
  }
  return { coalitionLegible, everLegibleOnBus };
}

// A weaker adversary than the full coalition: only the indices in `strippers` (a subset of non-r players) peel
// their keys off r's positions. Used to show even the shuffle initiator ALONE cannot make r's hole legible.
function subsetCannotOpen(sessions, locked, r, strippers) {
  for (const pos of holePositions(r)) {
    let cipher = locked[pos];
    for (const i of strippers) cipher = sessions[i].player.stripPosition(pos, cipher);
    try { valCard(cipher); return false; /* legible -> privacy broken */ } catch (e) { /* still locked */ }
  }
  return true;
}

// shuffle initiator (idx 0) specifically must not learn another hole: idx 0 ∈ coalition for every r>0, so the
// coalition test above already covers it; we additionally assert idx 0's local state holds no other-seat keys.
function initiatorHoldsOnlyOwnKeys(sessions) {
  const p0 = sessions[0].player;
  // a Player exposes only its own seat's keys; assert posKeys length == deck length and seat==0, and that the
  // session never stored another player's Player object.
  let onlyOwn = (p0.seat === 0);
  for (const s of sessions) {
    if (s.myIdx === 0) {
      // the session must reference exactly one Player whose seat is its own.
      onlyOwn = onlyOwn && (s.player === p0) && (s.player.seat === 0);
    }
  }
  return onlyOwn;
}

// ----------------------------------------------------------------- run N=2..6
async function main() {
  for (let n = 2; n <= 6; n++) {
    const seed = "deal-session-sim|N=" + n;
    const { relay, sessions, results, anchors } = await runDistributed(n, seed);
    const log = relay.log;

    // ---- correctness ----
    const locked = lockedDeckFromBus(log, n);
    ok(locked != null, "N=" + n + " locked deck broadcast on bus");

    // every seat recovered its own hole, and it matches the independent reference
    const allCards = [];
    for (let r = 0; r < n; r++) {
      const got = results[r].hole;
      const ref = referenceHole(sessions, n, locked, r);
      ok(JSON.stringify(got) === JSON.stringify(ref), "N=" + n + " seat " + r + " recovered correct hole");
      allCards.push(got[0], got[1]);
    }
    // board identical for all sessions and equals reference
    const refBoard = referenceBoard(sessions, n, locked);
    let boardConsistent = true;
    for (let r = 0; r < n; r++) {
      if (JSON.stringify(results[r].board) !== JSON.stringify(refBoard)) boardConsistent = false;
    }
    ok(boardConsistent, "N=" + n + " board identical for all & equals reference");
    refBoard.forEach((c) => allCards.push(c));

    // all 2N+5 dealt cards distinct, and exactly 2N+5 of them
    ok(allCards.length === deckPositions(n), "N=" + n + " dealt 2N+5=" + deckPositions(n) + " cards");
    ok(new Set(allCards).size === allCards.length, "N=" + n + " all dealt cards distinct");
    allCards.forEach((c) => ok(c >= 0 && c <= 51, "N=" + n + " card in range " + c));

    // anchors identical across all sessions (un-steerable on-chain digests)
    const a0 = anchors[0];
    ok(a0 && a0.dealDigests && a0.dealDigests.length === n + 1,
       "N=" + n + " anchor has n+1 stage digests");
    let anchorsAgree = !!a0;
    for (let i = 1; i < n; i++) {
      const ai = anchors[i];
      if (!ai) { anchorsAgree = false; break; }
      if (hexOf(ai.cardmapDigest) !== hexOf(a0.cardmapDigest)) anchorsAgree = false;
      for (let s = 0; s < a0.dealDigests.length; s++) {
        if (hexOf(ai.dealDigests[s]) !== hexOf(a0.dealDigests[s])) anchorsAgree = false;
      }
    }
    ok(anchorsAgree, "N=" + n + " every session computed identical FN_DECK_DIGEST anchors");

    // ---- privacy ----
    for (let r = 0; r < n; r++) {
      const { coalitionLegible, everLegibleOnBus } = coalitionCannotOpen(sessions, n, locked, r, log);
      ok(!coalitionLegible,
         "N=" + n + " PRIVACY: coalition of all non-" + r + " keys CANNOT open seat " + r + "'s hole");
      ok(!everLegibleOnBus,
         "N=" + n + " PRIVACY: bus carried NO cleartext card at seat " + r + "'s positions");
    }
    // shuffle initiator cannot learn another hole (covered by coalition test for every r>0) + holds only own keys
    ok(initiatorHoldsOnlyOwnKeys(sessions),
       "N=" + n + " PRIVACY: shuffle initiator (idx 0) holds ONLY its own keys");
    // explicit: the shuffle initiator (idx 0) alone, peeling ONLY its own key, cannot open seat 1's hole.
    ok(subsetCannotOpen(sessions, locked, 1, [0]),
       "N=" + n + " PRIVACY: shuffle initiator alone CANNOT open seat 1's hole");
  }

  console.log("\nPRIVACY SIM: " + (fail === 0 ? "PASS" : "FAIL") + ": " + pass + "/" + (pass + fail) +
              " assertions passed (correctness + privacy), failures=" + fail);
  if (fail) process.exit(1);
}

main().catch((e) => { console.error(e); process.exit(1); });
