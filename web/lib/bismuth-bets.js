// bismuth-bets.js — Stage-6 spectator parimutuel betting ABI helpers (doc/34), layered ADDITIVELY on top of
// bismuth-tx.js. Kept in a SEPARATE module so bismuth-tx.js (the audited player calldata builders + signer)
// is not edited. A spectator stakes BIS on a seated player winning the CURRENT hand; FN_SETTLE_SBETS pays the
// pool PARIMUTUEL from the contract's own showdown result (no oracle, no server).
//
// The side-pool lives in poker_table.py under tag domains DISJOINT from the player pot (0x30/0x31 vs the
// 0x11..0x21 player domains), with its OWN conservation (Σ payouts == S_SBET_POOL). These helpers only build
// calldata + decode the public storage read from GET /api/vm/contract; they never touch the player builders.

import { FN as PLAYER_FN, be4, concat, addr28FromHex } from "./bismuth-tx.js";

// ----------------------------------------------------------------- spectator ABI selectors (poker_table.py)
export const SBET_FN = {
  SPECTATE_BET: 13,   // FN_SPECTATE_BET(seat) + VALUE — stake on `seat` winning this hand (non-seated only)
  SETTLE_SBETS: 14,   // FN_SETTLE_SBETS() — permissionless, idempotent parimutuel settle / refund
};

// ----------------------------------------------------------------- side-pool storage ABI (poker_table.py)
const u32 = (x) => x >>> 0;
export const SBET_TAG = {
  SEAT: 0x30000000,   // | seat : Σ spectator stake backing each seat this hand (TAG_SBET_SEAT)
  REC:  0x31000000,   // | (seq<<4) | w : per-bet record (w0 seat, w1 amount, w2..8 = 28-byte addr) (TAG_SBET)
};
// global slots (poker_table.py S_SBET_*)
export const SBET_SLOT = { POOL: 21, CNT: 22, DONE: 23, CLOSE: 24, HANDNO: 25 };
// per-bet record word offsets
export const SBET_W = { SEAT: 0, AMT: 1, ADDR: 2 };

export const sbetSeatKey = (seat) => u32(SBET_TAG.SEAT | seat);
export const sbetRecordKey = (seq, w) => u32(SBET_TAG.REC | (seq << 4) | w);

// ----------------------------------------------------------------- calldata builders (be4(sel) ++ args)
// FN_SPECTATE_BET(seat, addr28) + VALUE — same calldata shape as FN_SIT: be4(sel) ++ be4(seat) ++ addr28.
// `addr28` is the bettor's full 28-byte identity (the contract authenticates word-6 == SYS_CALLER low-32 and
// rejects a SEATED player). VALUE (the BIS staked) attaches as the tx amount, exactly like FN_SIT.
export function spectateBetCalldata(seat, addr28) {
  if (!(addr28 instanceof Uint8Array) || addr28.length !== 28) throw new Error("addr28 must be 28 bytes");
  if (!(seat >= 0)) throw new Error("seat must be >= 0");
  return concat([be4(SBET_FN.SPECTATE_BET), be4(seat), addr28]);
}

export function spectateBetCalldataFromHex(seat, addrHex) {
  return spectateBetCalldata(seat, addr28FromHex(addrHex));
}

// FN_SETTLE_SBETS() — no args, no value (permissionless; anyone may settle once the hand is decided).
export function settleSbetsCalldata() {
  return be4(SBET_FN.SETTLE_SBETS);
}

// ----------------------------------------------------------------- read-side decoders (storage map T)
// T is a {int(key) -> int(value)} map built from GET /api/vm/contract/<addr>.storage (same as the table SPA).

export function sbetPool(T) { return T[SBET_SLOT.POOL] || 0; }
export function sbetCount(T) { return T[SBET_SLOT.CNT] || 0; }
export function sbetDone(T) { return (T[SBET_SLOT.DONE] || 0) !== 0; }
export function sbetClose(T) { return T[SBET_SLOT.CLOSE] || 0; }
export function sbetSeatStake(T, seat) { return T[sbetSeatKey(seat)] || 0; }

// Live parimutuel view for a seat: { stake, share (fraction of pool), odds (decimal multiple for a 1-unit
// bet if that seat wins now) }. Odds = pool / stake (parimutuel); undefined/Infinity-safe -> 0 when no stake.
export function seatParimutuel(T, seat) {
  const pool = sbetPool(T);
  const stake = sbetSeatStake(T, seat);
  const share = pool > 0 ? stake / pool : 0;
  const odds = stake > 0 ? pool / stake : 0;
  return { stake, pool, share, odds };
}

// Decode bet record `seq` -> { seq, seat, amount, addrHex } (self-describing, like read_sbet in poker_table.py).
export function readSbet(T, seq) {
  const g = (w) => (T[sbetRecordKey(seq, w)] || 0) >>> 0;
  let addr = "";
  for (let w = 0; w < 7; w++) addr += (g(SBET_W.ADDR + w) >>> 0).toString(16).padStart(8, "0");
  return { seq, seat: g(SBET_W.SEAT), amount: g(SBET_W.AMT), addrHex: addr };
}

// Fold the append-only records into a per-bettor summary keyed by addrHex (for "my bets" / settled P&L).
export function foldBets(T) {
  const n = sbetCount(T);
  const byAddr = {};
  const all = [];
  for (let seq = 0; seq < n; seq++) {
    const r = readSbet(T, seq);
    all.push(r);
    const k = r.addrHex;
    if (!byAddr[k]) byAddr[k] = { addrHex: k, total: 0, bySeat: {} };
    byAddr[k].total += r.amount;
    byAddr[k].bySeat[r.seat] = (byAddr[k].bySeat[r.seat] || 0) + r.amount;
  }
  return { records: all, byAddr };
}

export { be4, concat } from "./bismuth-tx.js";
export const PLAYER_SELECTORS = PLAYER_FN;
