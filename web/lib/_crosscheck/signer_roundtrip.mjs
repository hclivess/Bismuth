// signer_roundtrip.mjs — prove the JS signer (bismuth-tx.js canonical buffer + RSA-PKCS1/SHA-1 framing)
// produces a tx the NODE's own SignerFactory.verify_tx_signature accepts.
//
// node crypto's createSign('RSA-SHA1') is the EXACT primitive jsrsasign's 'SHA1withRSA' implements in the
// browser (RSASSA-PKCS1-v1_5 over SHA-1). We build the wire tx with bismuth-tx.js, then hand the 8-field
// tuple to verify_roundtrip.py (which calls the real node verifier).
//
// Run via run_crosscheck.sh (it generates a throwaway RSA wallet first).

import { createSign, createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import * as tx from "../bismuth-tx.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const wallet = JSON.parse(readFileSync(join(__dirname, "test_wallet.json"), "utf8"));

const pemPub = wallet["Public Key"];   // PEM string
const pemPriv = wallet["Private Key"]; // PEM string
const address = wallet["Address"];     // sha224(pemPub) hex

function b64(bytes) { return Buffer.from(bytes).toString("base64"); }

// Mirror keys_load_new: public_key wire field = base64(PEM public key utf8 string).
const publicKeyB64 = Buffer.from(pemPub, "utf8").toString("base64");

const signer = {
  address,
  publicKeyB64,
  signRsaSha1(buffer) {
    // RSASSA-PKCS1-v1_5 over SHA-1 (Crypto.Hash.SHA == SHA-1), then base64 (sign_buffer_for_bis).
    const s = createSign("RSA-SHA1");
    s.update(Buffer.from(buffer));
    s.end();
    const raw = s.sign(pemPriv); // PKCS1 v1.5 by default for RSA keys
    return raw.toString("base64");
  },
};

// Build a vm:call (FN_CHECK, no value) to a sample contract, with a fixed timestamp for reproducibility.
const contract = "00112233445566778899aabbccddeeff00112233445566778899aabb";
const calldataHex = tx.hex(tx.checkCalldata());
const built = tx.buildVmCall(signer, contract, calldataHex, { value: 0, timestamp: 1700000000.0 });

writeFileSync(join(__dirname, "signed_tx.json"), JSON.stringify({ tx: built.tx }, null, 1));
console.log("JS signer built + wrote signed_tx.json");
console.log("  address     :", address);
console.log("  operation   :", built.tx[6]);
console.log("  openfield   :", built.tx[7]);
console.log("  sig[:24]    :", built.tx[4].slice(0, 24), "...");
