// bismuth-tourney.js — calldata builders + read helpers for contracts/tournament.py (doc/28 §6, doc/32,
// Stage 5). The on-chain twin of tournament.py's ABI. Reuses the byte helpers + tx builders from
// bismuth-tx.js (be4 / concat / hex / addr28FromHex / buildVmCall) so the lobby targets the SAME wallet
// signer interface as the table SPA.
//
// Value (BIS) attaches ONLY to FN_REGISTER. Every other selector must be sent with value 0 (the contract
// runs _require_no_value and reverts on attached BIS). The betting selectors carry a LEADING tid.

import { be4, concat, hex, fromHex, addr28FromHex } from "./bismuth-tx.js";

// ----------------------------------------------------------------- tournament ABI selectors (tournament.py)
export const TFN = {
  REGISTER: 0,
  START: 1,
  DEAL: 2,
  COMMIT: 3,
  CHECK: 6,
  CALL: 7,
  BET: 8,
  FOLD: 9,
  REVEAL: 10,
  TIMEOUT: 11,
  SETTLE: 12,
  BUST: 13,
  FINALIZE: 14,
};

export const FMT_SNG = 0;
export const FMT_MTT = 1;

export const TPH = { REG: 0, RUNNING: 1, DONE: 2 };

// finisher ladders (permille; sum to 1000) — must match the schedule baked into the deployed bytecode
export const SCHED_WINNER_TAKE_ALL = [1000];
export const SCHED_SNG_TOP3 = [500, 300, 200];   // 50 / 30 / 20
export const SCHED_TOP2 = [650, 350];

// ----------------------------------------------------------------- calldata builders
// FN_REGISTER(addr28) + BUYIN: be4(sel) ++ addr28(28). Send with value == the tournament buy-in.
export function registerCalldata(addr28) {
  if (addr28.length !== 28) throw new Error("addr28 must be 28 bytes");
  return concat([be4(TFN.REGISTER), addr28]);
}

export function startCalldata() {
  return be4(TFN.START);
}

export function dealCalldata(tid) {
  return concat([be4(TFN.DEAL), be4(tid)]);
}

export function commitCalldata(tid, commit32) {
  if (commit32.length !== 32) throw new Error("commit must be 32 bytes");
  return concat([be4(TFN.COMMIT), be4(tid), commit32]);
}

export function checkCalldata(tid) { return concat([be4(TFN.CHECK), be4(tid)]); }
export function callCalldata(tid) { return concat([be4(TFN.CALL), be4(tid)]); }

// FN_BET(tid, target BE32): target is the total street_bet (chips) after the action.
export function betCalldata(tid, targetTotal) {
  return concat([be4(TFN.BET), be4(tid), be4(targetTotal)]);
}

export function foldCalldata(tid) { return concat([be4(TFN.FOLD), be4(tid)]); }

// FN_REVEAL(tid, c0..c4, nonce32)
export function revealCalldata(tid, cards5, nonce32) {
  if (cards5.length !== 5) throw new Error("need 5 cards");
  if (nonce32.length !== 32) throw new Error("nonce must be 32 bytes");
  const cb = new Uint8Array(5);
  for (let i = 0; i < 5; i++) {
    const c = Number(cards5[i]);
    if (!(c >= 0 && c <= 51)) throw new Error("card out of range 0..51");
    cb[i] = c;
  }
  return concat([be4(TFN.REVEAL), be4(tid), cb, nonce32]);
}

export function timeoutCalldata(tid) { return concat([be4(TFN.TIMEOUT), be4(tid)]); }
export function settleCalldata(tid) { return concat([be4(TFN.SETTLE), be4(tid)]); }
export function bustCalldata(tid) { return concat([be4(TFN.BUST), be4(tid)]); }
export function finalizeCalldata() { return be4(TFN.FINALIZE); }

// ----------------------------------------------------------------- storage key helpers (mirror tournament.py)
// Tournament globals (small slots)
export const S = {
  TPHASE: 1, PRIZE_POOL: 2, NREG: 3, NTABLES: 4, NALIVE: 5,
  NPLACES_PAID: 6, RUNNING_STARTED: 7, START_BLOCK: 8,
};

// per-entrant tags
const TAG_ENT_ADDR = 0x30000000;
const TAG_ENT_ALIVE = 0x31000000;
const TAG_ENT_PLACE = 0x32000000;
const TAG_ENT_TID = 0x33000000;
const TAG_ENT_SEAT = 0x34000000;
const TAG_ENT_PAID = 0x35000000;

// per-table region
const TID_BASE = 0x40000000;
const TID_STRIDE = 0x01000000;
export const TS = {
  PHASE: 0, DEADLINE: 1, BUTTON: 2, STREET: 3, TOACT: 4, TOCALL: 5,
  MINRAISE: 6, ESCROW: 7, WINBYFOLD: 8, NPOTS: 9, HANDNO: 10, SB: 11, BB: 12,
};
const TT_ADDR = 0x00100000, TT_STACK = 0x00200000, TT_STATE = 0x00500000,
      TT_POTAMT = 0x00C00000, TT_POTELIG = 0x00D00000, TT_ENTIDX = 0x00E00000;

// JS bitwise is 32-bit signed; use >>> 0 to read keys as unsigned. tid*STRIDE stays within 32 bits for
// tid < 256, so plain multiply is fine.
function region(tid) { return ((TID_BASE + tid * TID_STRIDE) >>> 0); }

export function tsKey(tid, slot) { return (region(tid) + slot) >>> 0; }
export function ttKey(tid, tag, sub) { return (region(tid) + tag + (sub || 0)) >>> 0; }

export function entAddrKey(e, w) { return (TAG_ENT_ADDR | (e << 4) | w) >>> 0; }
export function entAliveKey(e) { return (TAG_ENT_ALIVE | e) >>> 0; }
export function entPlaceKey(e) { return (TAG_ENT_PLACE | e) >>> 0; }
export function entTidKey(e) { return (TAG_ENT_TID | e) >>> 0; }
export function entSeatKey(e) { return (TAG_ENT_SEAT | e) >>> 0; }
export function entPaidKey(e) { return (TAG_ENT_PAID | e) >>> 0; }

export function stackKey(tid, seat) { return ttKey(tid, TT_STACK, seat); }
export function stateKey(tid, seat) { return ttKey(tid, TT_STATE, seat); }

// ----------------------------------------------------------------- read decoders (slot map -> objects)
// `slots` is a {keyInt: valueInt} map reconstructed from GET /api/vm/contract/{addr} (state-root, RO).
function g(slots, k) { return Number(slots[k] || slots[String(k)] || 0); }

// 28-byte address (56 hex) from the 7 stored words of an entrant.
export function readEntrantAddr(slots, e) {
  let s = "";
  for (let w = 0; w < 7; w++) {
    const word = g(slots, entAddrKey(e, w)) >>> 0;
    s += word.toString(16).padStart(8, "0");
  }
  return s;
}

export function readEntrant(slots, e) {
  return {
    e,
    addrHex: readEntrantAddr(slots, e),
    alive: g(slots, entAliveKey(e)),
    place: g(slots, entPlaceKey(e)),
    tid: g(slots, entTidKey(e)),
    seat: g(slots, entSeatKey(e)),
    paid: g(slots, entPaidKey(e)),
  };
}

// Full tournament snapshot for the lobby: phase, pool, entrant list, alive count, table count.
export function readTournament(slots) {
  const nreg = g(slots, S.NREG);
  const entrants = [];
  for (let e = 0; e < nreg; e++) entrants.push(readEntrant(slots, e));
  return {
    phase: g(slots, S.TPHASE),
    prizePool: g(slots, S.PRIZE_POOL),
    nreg,
    ntables: g(slots, S.NTABLES),
    nalive: g(slots, S.NALIVE),
    startBlock: g(slots, S.START_BLOCK),
    placesPaid: g(slots, S.NPLACES_PAID),
    runningStarted: g(slots, S.RUNNING_STARTED),
    entrants,
  };
}

// Blind level for a height given the (sb,bb) schedule + level_blocks the tournament was built with.
export function blindsAtHeight(levels, levelBlocks, startBlock, height) {
  if (height < startBlock) return levels[0];
  const L = Math.min(Math.floor((height - startBlock) / levelBlocks), levels.length - 1);
  return levels[L];
}

// Finisher ladder split (mirrors tournament_ref.payout_split): share_k = pool*permille_k // 1000, remainder
// to place 1. Returns BIS-unit integers summing exactly to pool.
export function payoutSplit(pool, schedule) {
  const sum = schedule.reduce((a, b) => a + b, 0);
  if (sum !== 1000) throw new Error("schedule permille must sum to 1000");
  const shares = schedule.map((p) => Math.floor((pool * p) / 1000));
  const rem = pool - shares.reduce((a, b) => a + b, 0);
  if (shares.length) shares[0] += rem;
  return shares;
}

export const TT = { ADDR: TT_ADDR, STACK: TT_STACK, STATE: TT_STATE, POTAMT: TT_POTAMT,
                    POTELIG: TT_POTELIG, ENTIDX: TT_ENTIDX };
