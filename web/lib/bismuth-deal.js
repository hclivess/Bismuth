// bismuth-deal.js — a faithful, byte-for-byte JS port of contracts/poker_deal.py (doc/28 §5):
// the N-party decentralized mental-poker DEAL for Bismuth Hold'em. BigInt SRA over the TRUE secp256k1
// field prime, the Barnett-Smart per-position locking round, victim-key-last hole opening + the
// refuse-without-own-key guard, the card<->field map, and the canonical _deck_bytes / cardmap_bytes
// serialization so FN_DECK_DIGEST hashes match the contract's stored anchors.
//
// Pure module: works in the browser (ES module) and under node. Crypto dependencies:
//   * SHA-256 for cardmap_digest / deal_digests — supplied via an injected `sha256(bytes)->bytes`
//     (WebCrypto in-browser is async, so hashing helpers are provided in two flavours; the pure
//     arithmetic + serialization need no hash and are synchronous).
//   * randomness — an injected `rng(nbytes)->Uint8Array`. Defaults to crypto.getRandomValues / node crypto.
//
// DEMO-GRADE ONLY (see poker_deal.py HONEST LIMITS [SEC-H1]): the secp256k1 field prime with the trivial
// card map; before real money this MUST move to a safe prime + QR encoding.

// ----------------------------------------------------------------- the TRUE secp256k1 field prime
// P = 2^256 - 2^32 - 977 (64 hex digits / 256 bits). The web demo's old literal (~index.html line 86) was a
// TRUNCATED non-prime — we use the exact constant so the SRA roundtrip is invertible.
export const P = (2n ** 256n) - (2n ** 32n) - 977n;
export const PM1 = P - 1n;

// Fermat sanity check (mirrors poker_deal.py's assert pow(2,P-1,P)==1).
if (modpow(2n, P - 1n, P) !== 1n) {
  throw new Error("P must be prime (Fermat check) for SRA to be invertible");
}

// ----------------------------------------------------------------- default randomness
function _defaultRng(n) {
  // Browser: crypto.getRandomValues (in chunks of 65536). Node: require('crypto').randomBytes.
  const g = (typeof globalThis !== "undefined") ? globalThis : {};
  if (g.crypto && typeof g.crypto.getRandomValues === "function") {
    const out = new Uint8Array(n);
    for (let off = 0; off < n; off += 65536) {
      g.crypto.getRandomValues(out.subarray(off, Math.min(off + 65536, n)));
    }
    return out;
  }
  // node fallback (only reached outside a browser)
  // eslint-disable-next-line global-require
  const nc = (typeof require === "function") ? require("crypto") : null;
  if (nc) return new Uint8Array(nc.randomBytes(n));
  throw new Error("no secure RNG available");
}

// ----------------------------------------------------------------- bytes <-> BigInt helpers
export function bytesToBigIntBE(bytes) {
  let x = 0n;
  for (let i = 0; i < bytes.length; i++) x = (x << 8n) | BigInt(bytes[i]);
  return x;
}

// Minimal big-endian byte representation of a non-negative BigInt (matches Python int.to_bytes with the
// minimal length used by _deck_bytes: (max(v,1).bit_length()+7)//8 or 1).
export function bigIntToMinBytesBE(v) {
  v = BigInt(v);
  if (v < 0n) throw new Error("negative");
  const bl = (v === 0n) ? 1n : bitLength(v);
  let len = Number((bl + 7n) / 8n);
  if (len < 1) len = 1;
  const out = new Uint8Array(len);
  let t = v;
  for (let i = len - 1; i >= 0; i--) { out[i] = Number(t & 0xffn); t >>= 8n; }
  return out;
}

export function bitLength(v) {
  v = BigInt(v);
  if (v < 0n) v = -v;
  let n = 0n;
  while (v > 0n) { v >>= 1n; n++; }
  return n;
}

// ----------------------------------------------------------------- modular arithmetic
export function modpow(base, exp, mod) {
  base = ((base % mod) + mod) % mod;
  let result = 1n;
  while (exp > 0n) {
    if (exp & 1n) result = (result * base) % mod;
    base = (base * base) % mod;
    exp >>= 1n;
  }
  return result;
}

function _egcd(a, b) {
  if (b === 0n) return [a, 1n, 0n];
  const [g, x, y] = _egcd(b, a % b);
  return [g, y, x - (a / b) * y];
}

function _modinv(a, m) {
  let aa = ((a % m) + m) % m;
  const [g, x] = _egcd(aa, m);
  if (g !== 1n) throw new Error("no modular inverse");
  return ((x % m) + m) % m;
}

// ----------------------------------------------------------------- card <-> field map
export function cardVal(c) {
  if (!(c >= 0 && c <= 51)) throw new Error("card out of range 0..51");
  return BigInt(c) + 2n;
}

export function valCard(x) {
  const c = Number(BigInt(x) - 2n);
  if (!(c >= 0 && c <= 51)) throw new Error("decrypted value " + x + " is not a legal card element");
  return c;
}

// Canonical serialization of the agreed card->value map (every player hashes this for the chain root).
// Mirrors poker_deal.cardmap_bytes EXACTLY: b"SRA-CARDMAP-v1|P=" + hex(P) + b"|" + "c:v," for c in 0..51.
// hex(P) in Python is "0x" + lowercase hex with NO leading zeros.
export function cardmapBytes() {
  const parts = [];
  parts.push(strBytes("SRA-CARDMAP-v1|P="));
  parts.push(strBytes("0x" + P.toString(16)));
  parts.push(strBytes("|"));
  for (let c = 0; c < 52; c++) {
    parts.push(strBytes(c + ":" + cardVal(c).toString() + ","));
  }
  return concatBytes(parts);
}

// ----------------------------------------------------------------- SRA key arithmetic
// gen_key: (k, kinv) with k in [2, P-2], gcd(k, P-1)==1, k*kinv == 1 (mod P-1).
// Mirrors poker_deal.gen_key: k = int.from_bytes(rng(32),'big') % (PM1-3) + 2; retry while gcd(k,PM1)!=1.
export function genKey(rng) {
  const rnd = rng || _defaultRng;
  for (;;) {
    const k = (bytesToBigIntBE(rnd(32)) % (PM1 - 3n)) + 2n;
    if (_egcd(k, PM1)[0] === 1n) {
      return [k, _modinv(k, PM1)];
    }
  }
}

export function enc(v, key) { return modpow(((BigInt(v) % P) + P) % P, key[0], P); }
export function dec(v, key) { return modpow(((BigInt(v) % P) + P) % P, key[1], P); }

// In-place Fisher-Yates, matching poker_deal._shuffle EXACTLY: j = int.from_bytes(rng(4),'big') % (i+1).
function _shuffle(arr, rng) {
  const rnd = rng || _defaultRng;
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Number(bytesToBigIntBE(rnd(4)) % BigInt(i + 1));
    const t = arr[i]; arr[i] = arr[j]; arr[j] = t;
  }
}

// ----------------------------------------------------------------- deck geometry
export function deckPositions(n) { return 2 * n + 5; }
export function holePositions(seat) { return [2 * seat, 2 * seat + 1]; }
export function boardPositions(n) {
  const out = [];
  for (let i = 2 * n; i < 2 * n + 5; i++) out.push(i);
  return out;
}

// ----------------------------------------------------------------- a player
export class Player {
  constructor(seat, n, rng) {
    this.seat = seat;
    this.n = n;
    this.rng = rng || null;
    this.shuffleKey = genKey(this.rng || _defaultRng); // k_p for the encrypt-shuffle round
    this.posKeys = null;                                // per-position keys, set at lock()
    this._perm = null;
  }

  // (1) encrypt-shuffle
  encryptShuffle(deck) {
    const idx = [];
    for (let i = 0; i < deck.length; i++) idx.push(i);
    _shuffle(idx, this.rng || _defaultRng);
    this._perm = idx;
    return idx.map((i) => enc(deck[i], this.shuffleKey));
  }

  // (2) Barnett-Smart per-position locking
  lock(deck) {
    const m = deck.length;
    this.posKeys = [];
    for (let i = 0; i < m; i++) this.posKeys.push(genKey(this.rng || _defaultRng));
    const out = [];
    for (let i = 0; i < m; i++) {
      const stripped = dec(deck[i], this.shuffleKey);  // remove k_p from position i
      out.push(enc(stripped, this.posKeys[i]));         // apply the per-position lock l_{p,i}
    }
    this.shuffleKey = null;
    return out;
  }

  // (3)/(4) partial decryption of one position
  stripPosition(position, cipher) {
    if (this.posKeys === null) throw new Error("must lock() before stripping positions");
    return dec(cipher, this.posKeys[position]);
  }

  // refuse-without-own-key guard
  assertNeedsMyKey(position, cipherAfterOthers) {
    let c;
    try {
      c = valCard(cipherAfterOthers);
    } catch (e) {
      return; // not a legal card value -> still locked under our key, as required
    }
    throw new Error(
      "refuse-without-own-key: position " + position + " already decrypts to card " + c +
      " WITHOUT my key l_{" + this.seat + "," + position + "}; a coalition could open my hole -> aborting reveal [SEC-H1]");
  }

  openMyHole(position, cipherAfterOthers) {
    this.assertNeedsMyKey(position, cipherAfterOthers);
    return valCard(this.stripPosition(position, cipherAfterOthers));
  }
}

// ----------------------------------------------------------------- the full deal
export class Deal {
  constructor(n, rng) {
    if (!(n >= 2 && n <= 9)) throw new Error("n must be 2..9");
    this.n = n;
    this.m = deckPositions(n);
    this.rng = rng || null;
    this.players = [];
    for (let s = 0; s < n; s++) this.players.push(new Player(s, n, this.rng));
    this.stageDecks = [];
    this.lockedDeck = null;
    this.shuffledDeck = null;
    this.holes = {};
    this.board = null;
  }

  // (1) encrypt-shuffle round S_0 -> ... -> S_{N-1}
  runShuffle() {
    let deck = [];
    for (let c = 0; c < 52; c++) deck.push(cardVal(c));
    for (const p of this.players) {
      deck = p.encryptShuffle(deck);
      this.stageDecks.push(deck.slice());
    }
    this.shuffledDeck = deck;
    return deck;
  }

  // (2) locking round
  runLock() {
    let deck = this.shuffledDeck;
    for (const p of this.players) deck = p.lock(deck);
    this.lockedDeck = deck;
    this.stageDecks.push(deck.slice());
    return deck;
  }

  // (3) deal seat r's hole (victim-key-last)
  dealHole(r) {
    const cards = [];
    for (const pos of holePositions(r)) {
      let cipher = this.lockedDeck[pos];
      for (const p of this.players) {
        if (p.seat !== r) cipher = p.stripPosition(pos, cipher);
      }
      cards.push(this.players[r].openMyHole(pos, cipher)); // victim strips LAST, with the guard
    }
    this.holes[r] = cards;
    return cards;
  }

  coalitionAttemptHole(r) {
    const residuals = [];
    for (const pos of holePositions(r)) {
      let cipher = this.lockedDeck[pos];
      for (const p of this.players) {
        if (p.seat !== r) cipher = p.stripPosition(pos, cipher);
      }
      residuals.push(cipher);
    }
    return residuals;
  }

  // (4) board: all live players partial-decrypt
  revealBoard(liveSeats) {
    const seats = liveSeats || range(this.n);
    const board = [];
    for (const pos of boardPositions(this.n)) {
      let cipher = this.lockedDeck[pos];
      for (const s of seats) cipher = this.players[s].stripPosition(pos, cipher);
      board.push(valCard(cipher));
    }
    this.board = board;
    return board;
  }

  revealBoardFor(_observerSeat) {
    const board = [];
    for (const pos of boardPositions(this.n)) {
      let cipher = this.lockedDeck[pos];
      for (const p of this.players) cipher = p.stripPosition(pos, cipher);
      board.push(valCard(cipher));
    }
    return board;
  }

  // anchoring digests — needs a sha256(bytes)->bytes(32). Use dealDigests (sync) with an injected hash, or
  // dealDigestsAsync with WebCrypto.
  stageDeckBytes() {
    return this.stageDecks.map((d) => deckBytes(d));
  }

  dealDigests(sha256) {
    return this.stageDecks.map((d) => sha256(deckBytes(d)));
  }

  async dealDigestsAsync(sha256async) {
    const out = [];
    for (const d of this.stageDecks) out.push(await sha256async(deckBytes(d)));
    return out;
  }
}

// Canonical big-endian serialization of a deck (list of field elements) for hashing — mirrors
// poker_deal._deck_bytes: for each v, 2-byte BE length prefix of the minimal big-endian bytes, then bytes.
export function deckBytes(deck) {
  const parts = [];
  for (const v of deck) {
    const b = bigIntToMinBytesBE(v);
    const lp = new Uint8Array(2);
    lp[0] = (b.length >> 8) & 0xff;
    lp[1] = b.length & 0xff;
    parts.push(lp);
    parts.push(b);
  }
  return concatBytes(parts);
}

export function cardmapDigest(sha256) { return sha256(cardmapBytes()); }
export async function cardmapDigestAsync(sha256async) { return await sha256async(cardmapBytes()); }

// ----------------------------------------------------------------- small byte utils
export function strBytes(s) {
  // ASCII/UTF-8 encode. The card-map text is pure ASCII, so TextEncoder is exact.
  if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(s);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i) & 0xff;
  return out;
}

export function concatBytes(arrs) {
  let total = 0;
  for (const a of arrs) total += a.length;
  const out = new Uint8Array(total);
  let off = 0;
  for (const a of arrs) { out.set(a, off); off += a.length; }
  return out;
}

export function bytesToHex(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += bytes[i].toString(16).padStart(2, "0");
  return s;
}

function range(n) { const a = []; for (let i = 0; i < n; i++) a.push(i); return a; }

// A deterministic sha256 byte-stream RNG matching tests/test_poker_deal.mkrng: st=sha256(st); concat until
// enough bytes. Seed is a Uint8Array/string. EXPOSED so the headless cross-check can reproduce the Python
// test vectors byte-for-byte (NOT for production randomness).
export function mkStreamRng(seed, sha256) {
  let st = (typeof seed === "string") ? strBytes(seed) : seed;
  return function rng(nbytes) {
    let out = new Uint8Array(0);
    while (out.length < nbytes) {
      st = sha256(st);
      out = concatBytes([out, st]);
    }
    return out.subarray(0, nbytes);
  };
}
