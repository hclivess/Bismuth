// wallet-core.js — the cryptographic core of the Bismuth browser wallet (doc/33 §5).
//
// This is the ONLY code that touches private key material. It runs inside the wallet ORIGIN (web/wallet/),
// never in a dApp page. It provides:
//   * BIP39 mnemonic (generate / validate / -> 64-byte seed) — byte-for-byte with bip39.py / hd_wallet.py.
//   * A DETERMINISTIC RSA account derived from the BIP39 seed: the seed feeds jsrsasign's keygen PRNG, so
//     the SAME mnemonic always reproduces the SAME classic-Bismuth RSA wallet (address = sha224 of the PEM,
//     signature = RSASSA-PKCS1-v1_5 over SHA-1 of the canonical tx buffer — the scheme the node validates,
//     confirmed against polysign SignerFactory.verify_bis_signature).
//   * At-rest encryption: passphrase -> PBKDF2-HMAC-SHA256 (high iteration) -> AES-GCM, stored in IndexedDB
//     of the wallet origin only. The seed/key NEVER leave this module (no API exports them).
//   * A signer object compatible with bismuth-tx.js (address, publicKeyB64, signRsaSha1(buffer)) plus a
//     signMessage(message) for the deal's e2e holepart encryption / pubkey-derivation use.
//
// jsrsasign (web/lib/vendor/jsrsasign-all-min.js) is loaded as a classic <script> before this module, so
// KJUR / KEYUTIL / RSAKey / hextob64 etc. are present as globals.
//
// DEMO-GRADE [SEC-H1]: deterministic RSA-from-mnemonic + the demo SRA prime are not-for-real-money; a
// production wallet would use the post-hf2 ECDSA/HD path. See doc/33 §8 threat 7.

const KDF_ITERS = 200000;          // PBKDF2-HMAC-SHA256 rounds for the at-rest passphrase
const DB_NAME = "bismuth-wallet";
const DB_STORE = "vault";
const DB_KEY = "encrypted-seed";

// ----------------------------------------------------------------- small byte utils
export function utf8(s) { return new TextEncoder().encode(s); }
export function hex(bytes) {
  let s = ""; for (let i = 0; i < bytes.length; i++) s += bytes[i].toString(16).padStart(2, "0"); return s;
}
export function fromHex(h) {
  h = h.replace(/^0x/, ""); const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(i * 2, 2), 16); return out;
}
function concatBytes(arrs) {
  let n = 0; for (const a of arrs) n += a.length;
  const out = new Uint8Array(n); let o = 0; for (const a of arrs) { out.set(a, o); o += a.length; } return out;
}
async function sha256(bytes) { return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)); }
async function sha224Hex(strU8) {
  // WebCrypto lacks SHA-224; jsrsasign provides it. Address = sha224(PEM string).hexdigest().
  const md = new KJUR.crypto.MessageDigest({ alg: "sha224", prov: "cryptojs" });
  md.updateHex(hex(strU8));
  return md.digest();
}

// ----------------------------------------------------------------- BIP39 (mirrors bip39.py)
let _WORDLIST = null;
export async function loadWordlist(url) {
  if (_WORDLIST) return _WORDLIST;
  const txt = await (await fetch(url || "bip39_english.txt")).text();
  const words = txt.split(/\r?\n/).map((w) => w.trim()).filter((w) => w.length);
  if (words.length !== 2048) throw new Error("BIP39 wordlist must have 2048 words, got " + words.length);
  _WORDLIST = words;
  return words;
}

function _bitsOfBytes(bytes) {
  let s = ""; for (const b of bytes) s += b.toString(2).padStart(8, "0"); return s;
}

export async function generateMnemonic(strengthBits) {
  strengthBits = strengthBits || 128;                 // 128 -> 12 words, 256 -> 24
  if (![128, 160, 192, 224, 256].includes(strengthBits)) throw new Error("bad strength");
  const entropy = new Uint8Array(strengthBits / 8);
  crypto.getRandomValues(entropy);
  return entropyToMnemonic(entropy);
}

export async function entropyToMnemonic(entropy) {
  const words = await loadWordlist();
  const csBits = entropy.length * 8 / 32;
  const checksum = await sha256(entropy);
  const bits = _bitsOfBytes(entropy) + _bitsOfBytes(checksum).slice(0, csBits);
  const out = [];
  for (let i = 0; i < bits.length; i += 11) out.push(words[parseInt(bits.slice(i, i + 11), 2)]);
  return out.join(" ");
}

export async function checkMnemonic(mnemonic) {
  try { await mnemonicToEntropy(mnemonic); return true; } catch (e) { return false; }
}

export async function mnemonicToEntropy(mnemonic) {
  const words = await loadWordlist();
  const idx = {}; words.forEach((w, i) => { idx[w] = i; });
  const toks = mnemonic.normalize("NFKD").trim().split(/\s+/).filter((w) => w);
  if (![12, 15, 18, 21, 24].includes(toks.length)) throw new Error("mnemonic must be 12/15/18/21/24 words");
  let bits = "";
  for (const w of toks) { if (!(w in idx)) throw new Error("word not in wordlist: " + w); bits += idx[w].toString(2).padStart(11, "0"); }
  const total = toks.length * 11;
  const entBits = Math.floor(total * 32 / 33);
  const csBits = total - entBits;
  const entropy = new Uint8Array(entBits / 8);
  for (let i = 0; i < entropy.length; i++) entropy[i] = parseInt(bits.slice(i * 8, i * 8 + 8), 2);
  const expected = _bitsOfBytes(await sha256(entropy)).slice(0, csBits);
  if (bits.slice(entBits) !== expected) throw new Error("invalid mnemonic checksum");
  return entropy;
}

// 64-byte BIP39 seed: PBKDF2-HMAC-SHA512(NFKD(mnemonic), "mnemonic"+passphrase, 2048, 64) — bip39.to_seed.
export async function mnemonicToSeed(mnemonic, passphrase) {
  const m = utf8(mnemonic.normalize("NFKD"));
  const salt = utf8(("mnemonic" + (passphrase || "")).normalize("NFKD"));
  const baseKey = await crypto.subtle.importKey("raw", m, "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations: 2048, hash: "SHA-512" }, baseKey, 512);
  return new Uint8Array(bits);
}

// ----------------------------------------------------------------- deterministic RSA from the seed
// Feed jsrsasign's SecureRandom from a seeded SHA-256 byte-stream so the SAME mnemonic -> SAME RSA key.
// (Demo-grade key derivation [SEC-H1]; the classic Bismuth wallet is RSA and the node validates RSA here.)
function _seededStream(seedBytes) {
  let st = seedBytes.slice();
  let pool = new Uint8Array(0);
  let pi = 0;
  return async function next(n) {
    const out = new Uint8Array(n);
    for (let i = 0; i < n; i++) {
      if (pi >= pool.length) { st = await sha256(st); pool = st; pi = 0; }
      out[i] = pool[pi++];
    }
    return out;
  };
}

// jsrsasign keygen is synchronous and pulls bytes via SecureRandom.prototype.nextBytes (also sync). We
// pre-expand a deterministic byte pool large enough for a 2048-bit keygen and serve it synchronously.
async function _rsaFromSeed(seedBytes) {
  // domain-separate the RSA account from the raw BIP39 seed so other future account types can coexist.
  const acctSeed = await sha256(concatBytes([utf8("bismuth-rsa-account-v1"), seedBytes]));
  const stream = _seededStream(acctSeed);
  const POOL = await stream(4096);                     // ample entropy for RSA-2048 keygen
  let pos = 0;
  const saved = KJUR.crypto.Util && null;              // noop ref to keep linters quiet
  const SR = SecureRandom;
  const orig = SR.prototype.nextBytes;
  SR.prototype.nextBytes = function (ba) {
    for (let i = 0; i < ba.length; i++) { ba[i] = POOL[pos % POOL.length]; pos++; }
  };
  let kp;
  try {
    kp = KEYUTIL.generateKeypair("RSA", 2048);
  } finally {
    SR.prototype.nextBytes = orig;
  }
  const prvPem = KEYUTIL.getPEM(kp.prvKeyObj, "PKCS8PRV");
  const pubPem = KEYUTIL.getPEM(kp.pubKeyObj);          // SPKI "-----BEGIN PUBLIC KEY-----"
  const address = await sha224Hex(utf8(pubPem));        // matches SignerRsa.public_key_to_address
  return { prvPem, pubPem, address, prvKeyObj: kp.prvKeyObj };
}

// ----------------------------------------------------------------- the in-memory account / signer
export class Account {
  constructor(rsa, mnemonic, seed) {
    this._rsa = rsa;                 // {prvPem, pubPem, address, prvKeyObj}
    this._mnemonic = mnemonic;       // kept only while unlocked, for backup re-display
    this._seed = seed;
    this.address = rsa.address;      // 56-hex sha224 address
    this.publicKeyB64 = btoa(rsa.pubPem);  // base64(PEM string) — the wire public_key field
  }

  // bismuth-tx.js signer contract: signRsaSha1(buffer Uint8Array) -> base64 raw RSASSA-PKCS1-v1_5(SHA1).
  signRsaSha1(buffer) {
    const sig = new KJUR.crypto.Signature({ alg: "SHA1withRSA" });
    sig.init(this._rsa.prvKeyObj);
    sig.updateHex(hex(buffer instanceof Uint8Array ? buffer : utf8(String(buffer))));
    const sigHex = sig.sign();
    // base64 of the RAW signature bytes (sign_buffer_for_bis: b64encode(raw)).
    return btoa(String.fromCharCode.apply(null, fromHex(sigHex)));
  }

  // Deterministic per-message signature (used by the deal for e2e key derivation / authentication).
  // Returns {signature(b64), public_key(b64)}; never the key. (doc/33 §5: signMessage, not key export.)
  signMessage(message) {
    const buf = message instanceof Uint8Array ? message : utf8(String(message));
    return { signature: this.signRsaSha1(buf), public_key: this.publicKeyB64 };
  }

  mnemonic() { return this._mnemonic; }  // shown ONCE on creation; never sent off-origin
}

// ----------------------------------------------------------------- at-rest vault (IndexedDB + AES-GCM)
function _openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(DB_STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function _dbGet(key) {
  const db = await _openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, "readonly").objectStore(DB_STORE).get(key);
    tx.onsuccess = () => resolve(tx.result); tx.onerror = () => reject(tx.error);
  });
}
async function _dbPut(key, val) {
  const db = await _openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, "readwrite").objectStore(DB_STORE).put(val, key);
    tx.onsuccess = () => resolve(); tx.onerror = () => reject(tx.error);
  });
}
async function _dbDel(key) {
  const db = await _openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DB_STORE, "readwrite").objectStore(DB_STORE).delete(key);
    tx.onsuccess = () => resolve(); tx.onerror = () => reject(tx.error);
  });
}

async function _deriveAesKey(passphrase, salt) {
  const base = await crypto.subtle.importKey("raw", utf8(passphrase), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations: KDF_ITERS, hash: "SHA-256" },
    base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
}

export async function hasVault() { return !!(await _dbGet(DB_KEY)); }
export async function destroyVault() { await _dbDel(DB_KEY); }

// Persist the mnemonic (the seed material) encrypted under the passphrase. We store the MNEMONIC (not the
// RSA key) so the wallet re-derives the same RSA account on unlock — the mnemonic is the single backup.
async function _encryptAndStore(mnemonic, passphrase) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const aes = await _deriveAesKey(passphrase, salt);
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, aes, utf8(mnemonic)));
  await _dbPut(DB_KEY, { v: 1, salt: hex(salt), iv: hex(iv), ct: hex(ct), iters: KDF_ITERS });
}

// ----------------------------------------------------------------- high-level wallet lifecycle
export class Wallet {
  constructor() { this.account = null; }

  get locked() { return this.account === null; }

  // Create a brand-new wallet: fresh mnemonic, derive the RSA account, encrypt at rest. Returns the mnemonic
  // ONCE so the UI can show it for backup. The mnemonic is not persisted in plaintext anywhere.
  async create(passphrase, strengthBits) {
    const mnemonic = await generateMnemonic(strengthBits || 128);
    const seed = await mnemonicToSeed(mnemonic, "");
    const rsa = await _rsaFromSeed(seed);
    await _encryptAndStore(mnemonic, passphrase);
    this.account = new Account(rsa, mnemonic, seed);
    return { mnemonic, address: rsa.address };
  }

  // Restore from a user-supplied mnemonic (import an existing wallet).
  async restore(mnemonic, passphrase) {
    mnemonic = mnemonic.normalize("NFKD").trim().replace(/\s+/g, " ");
    if (!(await checkMnemonic(mnemonic))) throw new Error("invalid BIP39 mnemonic (typo or bad checksum)");
    const seed = await mnemonicToSeed(mnemonic, "");
    const rsa = await _rsaFromSeed(seed);
    await _encryptAndStore(mnemonic, passphrase);
    this.account = new Account(rsa, mnemonic, seed);
    return { address: rsa.address };
  }

  // Unlock the existing at-rest vault with the passphrase; re-derives the RSA account in memory.
  async unlock(passphrase) {
    const rec = await _dbGet(DB_KEY);
    if (!rec) throw new Error("no wallet stored; create or restore first");
    const salt = fromHex(rec.salt), iv = fromHex(rec.iv), ct = fromHex(rec.ct);
    const aes = await _deriveAesKey(passphrase, salt);
    let mnemonic;
    try {
      mnemonic = new TextDecoder().decode(await crypto.subtle.decrypt({ name: "AES-GCM", iv }, aes, ct));
    } catch (e) {
      throw new Error("wrong passphrase");
    }
    const seed = await mnemonicToSeed(mnemonic, "");
    const rsa = await _rsaFromSeed(seed);
    this.account = new Account(rsa, mnemonic, seed);
    return { address: rsa.address };
  }

  lock() { this.account = null; }

  requireUnlocked() { if (this.locked) throw new Error("wallet locked"); return this.account; }
}
