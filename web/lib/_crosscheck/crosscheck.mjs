// crosscheck.mjs — node cross-check: assert web/lib/bismuth-deal.js reproduces poker_deal.py's vectors
// EXACTLY, and unit-test web/lib/bismuth-tx.js calldata builders against poker_table.py's FN_* selectors.
//
// Run:  node web/lib/_crosscheck/crosscheck.mjs
// (after python3 web/lib/_crosscheck/dump_vectors.py has written vectors.json)
//
// The JS deal is driven by the SAME deterministic sha256-stream RNG (same seed, same call order) as the
// Python reference, so every stage deck, hole, board, and deal-digest must match byte-for-byte.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import * as deal from "../bismuth-deal.js";
import * as tx from "../bismuth-tx.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

function sha256(bytes) {
  return new Uint8Array(createHash("sha256").update(Buffer.from(bytes)).digest());
}

let failures = 0;
let checks = 0;
function ok(cond, label) {
  checks++;
  if (cond) { /* console.log("ok   " + label); */ }
  else { failures++; console.error("FAIL " + label); }
}
function eqHex(a, b, label) { ok(a === b, label + (a === b ? "" : "  got=" + a + " exp=" + b)); }

const V = JSON.parse(readFileSync(join(__dirname, "vectors.json"), "utf8"));

// ----------------------------------------------------------------- 1. prime
ok(deal.P.toString() === V.P_dec, "prime P decimal");
eqHex("0x" + deal.P.toString(16), V.P_hex, "prime P hex");

// ----------------------------------------------------------------- 2. card map + cardmap bytes/digest
for (const [c, v] of V.card_map) {
  ok(deal.cardVal(c).toString() === v, "card_val(" + c + ")");
  ok(deal.valCard(BigInt(v)) === c, "val_card inverse for card " + c);
}
eqHex(deal.bytesToHex(deal.cardmapBytes()), V.cardmap_bytes_hex, "cardmap_bytes");
eqHex(deal.bytesToHex(deal.cardmapDigest(sha256)), V.cardmap_digest_hex, "cardmap_digest");

// ----------------------------------------------------------------- 3. enc/dec all 52 under the fixed key
{
  // Reproduce the fixed key the SAME way: gen_key over mkStreamRng("fixed-key-witness").
  const rng = deal.mkStreamRng("fixed-key-witness", sha256);
  const key = deal.genKey(rng);
  eqHex(key[0].toString(), V.fixed_key[0], "fixed key k");
  eqHex(key[1].toString(), V.fixed_key[1], "fixed key kinv");
  for (const [c, e] of V.enc_all_fixed_key) {
    const got = deal.enc(deal.cardVal(c), key);
    ok(got.toString() === e, "enc card " + c);
    ok(deal.dec(got, key).toString() === deal.cardVal(c).toString(), "dec roundtrip card " + c);
  }
}

// ----------------------------------------------------------------- 4. full deals N=2..6, driven identically
for (let n = 2; n <= 6; n++) {
  const want = V.deals[String(n)];
  const rng = deal.mkStreamRng(want.seed, sha256);
  const D = new deal.Deal(n, rng);
  D.runShuffle();
  D.runLock();
  const holes = {};
  for (let r = 0; r < n; r++) holes[r] = D.dealHole(r);
  const board = D.revealBoard();

  // stage decks (each as decimal strings)
  ok(D.stageDecks.length === want.stage_decks.length, "N=" + n + " stage count");
  for (let s = 0; s < want.stage_decks.length; s++) {
    const got = D.stageDecks[s].map((x) => x.toString());
    ok(JSON.stringify(got) === JSON.stringify(want.stage_decks[s]), "N=" + n + " stage deck " + s);
  }
  // shuffled + locked decks
  ok(JSON.stringify(D.shuffledDeck.map((x) => x.toString())) === JSON.stringify(want.shuffled_deck),
     "N=" + n + " shuffled deck");
  ok(JSON.stringify(D.lockedDeck.map((x) => x.toString())) === JSON.stringify(want.locked_deck),
     "N=" + n + " locked deck");
  // deal_digests
  const digs = D.dealDigests(sha256).map((h) => deal.bytesToHex(h));
  ok(JSON.stringify(digs) === JSON.stringify(want.deal_digests_hex), "N=" + n + " deal_digests");
  // holes + board
  for (let r = 0; r < n; r++) {
    ok(JSON.stringify(holes[r]) === JSON.stringify(want.holes[String(r)]), "N=" + n + " hole seat " + r);
  }
  ok(JSON.stringify(board) === JSON.stringify(want.board), "N=" + n + " board");
  // board reconstructs identically for every observer
  for (let r = 0; r < n; r++) {
    ok(JSON.stringify(D.revealBoardFor(r)) === JSON.stringify(board), "N=" + n + " board observer " + r);
  }
  // coalition cannot open a victim (residuals are not legal cards)
  for (let r = 0; r < n; r++) {
    const residuals = D.coalitionAttemptHole(r);
    for (const v of residuals) {
      let illegal = false;
      try { deal.valCard(v); } catch (e) { illegal = true; }
      ok(illegal, "N=" + n + " coalition cannot read seat " + r);
    }
  }
}

// ----------------------------------------------------------------- 5. calldata builders vs poker_table.py FN_*
// FN_* numbers read straight from poker_table.py (verified below by a sidecar python emit).
const FN_EXPECT = JSON.parse(readFileSync(join(__dirname, "fn_selectors.json"), "utf8"));
ok(tx.FN.SIT === FN_EXPECT.FN_SIT, "FN_SIT");
ok(tx.FN.LEAVE === FN_EXPECT.FN_LEAVE, "FN_LEAVE");
ok(tx.FN.DEAL === FN_EXPECT.FN_DEAL, "FN_DEAL");
ok(tx.FN.COMMIT === FN_EXPECT.FN_COMMIT, "FN_COMMIT");
ok(tx.FN.DECK_DIGEST === FN_EXPECT.FN_DECK_DIGEST, "FN_DECK_DIGEST");
ok(tx.FN.CHECK === FN_EXPECT.FN_CHECK, "FN_CHECK");
ok(tx.FN.CALL === FN_EXPECT.FN_CALL, "FN_CALL");
ok(tx.FN.BET === FN_EXPECT.FN_BET, "FN_BET");
ok(tx.FN.FOLD === FN_EXPECT.FN_FOLD, "FN_FOLD");
ok(tx.FN.REVEAL === FN_EXPECT.FN_REVEAL, "FN_REVEAL");
ok(tx.FN.TIMEOUT === FN_EXPECT.FN_TIMEOUT, "FN_TIMEOUT");
ok(tx.FN.SETTLE === FN_EXPECT.FN_SETTLE, "FN_SETTLE");
ok(tx.IDX_CARDMAP === FN_EXPECT.IDX_CARDMAP, "IDX_CARDMAP");
ok(tx.DECKH_MAXIDX === FN_EXPECT.DECKH_MAXIDX, "DECKH_MAXIDX");

// calldata byte layout = be4(selector) ++ args, matching test_poker_deal.be4/addr28 conventions.
{
  const addr = new Uint8Array(28); addr[27] = 0xA0;
  eqHex(tx.hex(tx.sitCalldata(3, addr)),
        "00000000" + "00000003" + tx.hex(addr), "FN_SIT calldata");
  eqHex(tx.hex(tx.leaveCalldata(2)), "00000001" + "00000002", "FN_LEAVE calldata");
  eqHex(tx.hex(tx.dealCalldata()), "00000002", "FN_DEAL calldata");
  const commit = new Uint8Array(32).fill(0xAB);
  eqHex(tx.hex(tx.commitCalldata(commit)), "00000003" + tx.hex(commit), "FN_COMMIT calldata");
  const h32 = sha256(new Uint8Array([120])); // sha256(b"x")
  eqHex(tx.hex(tx.deckDigestCalldata(5, h32)), "00000004" + "00000005" + tx.hex(h32), "FN_DECK_DIGEST calldata");
  eqHex(tx.hex(tx.deckDigestCalldata(tx.IDX_CARDMAP, h32)),
        "00000004" + "10000000" + tx.hex(h32), "FN_DECK_DIGEST cardmap calldata");
  eqHex(tx.hex(tx.checkCalldata()), "00000006", "FN_CHECK calldata");
  eqHex(tx.hex(tx.callCalldata()), "00000007", "FN_CALL calldata");
  eqHex(tx.hex(tx.betCalldata(250)), "00000008" + "000000fa", "FN_BET calldata");
  eqHex(tx.hex(tx.foldCalldata()), "00000009", "FN_FOLD calldata");
  const nonce = new Uint8Array(32).fill(0x11);
  const cards = [3, 14, 25, 40, 51];
  const expCards = cards.map((c) => c.toString(16).padStart(2, "0")).join("");
  eqHex(tx.hex(tx.revealCalldata(cards, nonce)), "0000000a" + expCards + tx.hex(nonce), "FN_REVEAL calldata");
}

// ----------------------------------------------------------------- 6. signing-buffer shape (python repr)
{
  // compare against a python-emitted reference buffer for a known vm:call
  const ref = JSON.parse(readFileSync(join(__dirname, "sign_ref.json"), "utf8"));
  const buf = tx.signatureBuffer(ref.timestamp, ref.address, ref.recipient, ref.amount, ref.operation, ref.openfield);
  eqHex(tx.hex(buf), ref.buffer_hex, "signature_buffer repr matches python");
}

console.log("\n" + (failures === 0 ? "PASS" : "FAIL") + ": " + (checks - failures) + "/" + checks + " checks passed");
if (failures) process.exit(1);
