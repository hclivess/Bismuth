// bismuth-tx.js — Bismuth transaction builder + JS signer (canonical buffer + RSA-SHA1 framing).
//
// SIGNING SCHEME (mirrors the node, verified against polysign + _lite_client):
//   The node validates a tx via mempool.merge -> SignerFactory.verify_tx_signature. For the CLASSIC
//   (pre-hf2 / RSA) path that the demo relays use (essentials.sign_rsa via _lite_client.LiteClient.send):
//
//     buffer    = repr( (timestamp, address, recipient, amount, operation, openfield) ).encode("utf-8")
//                 where the caller passes the CANONICAL string forms:
//                   timestamp = "%.2f" % time.time()      (2-decimal seconds, as a STRING)
//                   amount    = "%.8f" % float(amount)     (8-decimal BIS, as a STRING)
//                 and address/recipient/operation/openfield are already strings.
//                 repr() of a Python tuple of strings => "('<ts>', '<addr>', '<recip>', '<amt>', '<op>', '<of>')"
//                 (single-quoted, ", "-separated, with a trailing-comma ONLY for a 1-tuple — never here, 6 fields).
//     digest    = SHA-1(buffer)                            (PyCryptodome Crypto.Hash.SHA == SHA-1, OID 1.3.14.3.2.26)
//     raw_sig   = RSASSA-PKCS1-v1_5_sign(privkey, digest)  (PKCS1_v1_5)
//     signature = base64( raw_sig )                        (sign_buffer_for_bis: single b64 of the raw sig)
//     public_key= base64( PEM_public_key_string )          (keys_load_new: b64encode(public_key_readable.utf8))
//     address   = sha224( PEM_public_key_string ).hexdigest()  (56 hex; the RSA address)
//
//   The wire tx is the 8-field tuple, field order = bismuth_serialize.TX_FIELDS:
//     [timestamp, address, recipient, amount, signature, public_key, operation, openfield]
//   where timestamp and amount on the wire are the SAME canonical strings used in the buffer.
//
//   Submission: POST /api/transaction (rest_api_write) with body {"transaction": [<8 fields>]} — the SAME
//   mempool.merge validation as the socket mpinsert path; just a different transport.
//
// WebCrypto note: SubtleCrypto's RSASSA-PKCS1-v1_5 does NOT offer SHA-1, but the classic Bismuth scheme
// signs SHA-1. So the actual RSA-SHA1 signing primitive is INJECTED (e.g. jsrsasign's KJUR in-browser, or
// a node 'crypto' Sign('RSA-SHA1') in tests). This module owns the canonical buffer, the b64/hash framing,
// the tx tuple assembly; it delegates only the raw RSA-SHA1 + sha224(pubkey).

// ----------------------------------------------------------------- byte helpers
export function be4(x) {
  x = (Number(x) >>> 0);
  return new Uint8Array([(x >>> 24) & 0xff, (x >>> 16) & 0xff, (x >>> 8) & 0xff, x & 0xff]);
}

// 8-byte big-endian (value/amount byte work).
export function be8(x) {
  const v = BigInt(x);
  const out = new Uint8Array(8);
  let t = v;
  for (let i = 7; i >= 0; i--) { out[i] = Number(t & 0xffn); t >>= 8n; }
  return out;
}

export function concat(arrs) {
  let total = 0;
  for (const a of arrs) total += a.length;
  const out = new Uint8Array(total);
  let off = 0;
  for (const a of arrs) { out.set(a, off); off += a.length; }
  return out;
}

export function hex(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += bytes[i].toString(16).padStart(2, "0");
  return s;
}

export function fromHex(h) {
  h = h.replace(/^0x/, "");
  if (h.length % 2) throw new Error("odd hex length");
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(i * 2, 2), 16);
  return out;
}

// 28-byte address from a hex string (Bismuth RSA address is 56 hex = 28 bytes).
export function addr28FromHex(addrHex) {
  const b = fromHex(addrHex);
  if (b.length !== 28) throw new Error("address must be 28 bytes (56 hex), got " + b.length);
  return b;
}

// ----------------------------------------------------------------- the canonical signing buffer
// repr of a Python tuple of 6 strings. Python repr of a str uses single quotes unless the string itself
// contains a single quote and no double quote (then it switches to double quotes). The six canonical tx
// fields (timestamp/amount are numeric strings; address/recipient are hex; operation/openfield are short
// ASCII) never contain quotes, so single-quote framing is exact. We still implement Python's
// quote-selection rule defensively so any unexpected field is encoded identically to CPython repr.
export function pyReprStr(s) {
  s = String(s);
  const hasSingle = s.indexOf("'") !== -1;
  const hasDouble = s.indexOf('"') !== -1;
  let quote = "'";
  if (hasSingle && !hasDouble) quote = '"';
  let out = quote;
  for (const ch of s) {
    const code = ch.codePointAt(0);
    if (ch === "\\") out += "\\\\";
    else if (ch === quote) out += "\\" + quote;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (code < 0x20 || code === 0x7f) out += "\\x" + code.toString(16).padStart(2, "0");
    else out += ch;
  }
  out += quote;
  return out;
}

// buffer = repr((ts, addr, recip, amount, op, openfield)).encode("utf-8")
export function signatureBuffer(timestamp, address, recipient, amount, operation, openfield) {
  const inner = [timestamp, address, recipient, amount, operation, openfield].map(pyReprStr).join(", ");
  const s = "(" + inner + ")";
  return (typeof TextEncoder !== "undefined") ? new TextEncoder().encode(s) : _utf8(s);
}

function _utf8(s) {
  // minimal UTF-8 encoder fallback
  const out = [];
  for (const ch of s) {
    let c = ch.codePointAt(0);
    if (c < 0x80) out.push(c);
    else if (c < 0x800) { out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f)); }
    else if (c < 0x10000) { out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f)); }
    else { out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 0x3f), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f)); }
  }
  return new Uint8Array(out);
}

// Canonical field formatters matching essentials.sign_rsa / "%.2f","%.8f".
export function fmtTimestamp(secondsFloat) {
  return Number(secondsFloat).toFixed(2);
}
export function fmtAmount(amount) {
  // Byte-exact "%.8f" for any Bismuth amount. The signature is computed over this string, so a single
  // differing digit makes the node reject the tx — we MUST match Python exactly. Valid Bismuth amounts are
  // chain-quantized to <=8 decimals, so we format via decimal-string math (no float rounding divergence
  // between JS toFixed and Python "%.8f"). Accepts a number or, preferably, a decimal string (exact).
  let s = (typeof amount === "string" ? amount : Number(amount).toString()).trim();
  let neg = false;
  if (s[0] === "+") s = s.slice(1);
  else if (s[0] === "-") { neg = true; s = s.slice(1); }
  if (/[eE]/.test(s)) s = Number(s).toFixed(8);          // rare exponent form -> fall back (then re-split)
  let [intPart = "0", frac = ""] = s.split(".");
  if (frac.length > 8) {                                  // never true for a valid amount; guard anyway
    throw new Error("amount has more than 8 decimal places: " + amount);
  }
  frac = (frac + "00000000").slice(0, 8);                 // pad to exactly 8 fractional digits
  intPart = intPart.replace(/^0+(?=\d)/, "") || "0";
  const out = intPart + "." + frac;
  return (neg && !/^0\.0{8}$/.test(out)) ? "-" + out : out;   // no "-0.00000000"
}

// ----------------------------------------------------------------- tx assembly
// A "signer" is an object exposing:
//   signer.address            -> the 56-hex RSA address (sha224 of the PEM public key)
//   signer.publicKeyB64       -> base64(PEM public key string)  (the wire `public_key` field)
//   signer.signRsaSha1(buffer)-> base64(raw RSASSA-PKCS1-v1_5(SHA1(buffer)))  (the wire `signature` field)
// In the browser, signer.signRsaSha1 is backed by jsrsasign (KJUR.crypto.Signature 'SHA1withRSA');
// in node tests, by crypto.createSign('RSA-SHA1'). This module never touches the private key.

// Signed tx. operation/openfield supplied by caller (plain transfer, token:transfer, alias=..., ...).
export function buildTx(signer, recipient, value, operation, openfield, opts) {
  opts = opts || {};
  const timestamp = fmtTimestamp(opts.timestamp != null ? opts.timestamp : (Date.now() / 1000));
  const amount = fmtAmount(value);
  const address = signer.address;
  const buffer = signatureBuffer(timestamp, address, recipient, amount, operation || "", openfield || "");
  const signature = signer.signRsaSha1(buffer);
  const publicKey = signer.publicKeyB64;
  const tx = [timestamp, address, recipient, amount, signature, publicKey, operation || "", openfield || ""];
  return { tx, buffer, txidPrefix: signature.slice(0, 56) };
}

// POST body for rest_api_write POST /api/transaction.
export function submitBody(tx) {
  return JSON.stringify({ transaction: tx });
}
