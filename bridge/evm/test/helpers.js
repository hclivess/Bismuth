"use strict";
// Shared helpers for the wBIS-bridge Hardhat suite. Everything here mirrors the cross-chain oracle
// (oracle/gen_vectors.py -> fixtures/vectors.json) so the Solidity side is asserted byte-for-byte.

const fs = require("fs");
const path = require("path");
const { ethers } = require("hardhat");

const vectors = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "fixtures", "vectors.json"), "utf8")
);

// The Bismuth vault address committed in each leaf preimage is the ASCII of the 56-char hex STRING
// (56 bytes), NOT the decoded 28 bytes. See SPEC §3.1 (`V_ascii(56 bytes)`).
function vaultAddressBytes(hexString) {
  return "0x" + Buffer.from(hexString, "ascii").toString("hex");
}

// Oracle proof element {sibling, siblingIsLeft} -> Solidity Step tuple [sibling, isLeft, present].
// A null sibling is a vm_merkle "promoted" level: sibling=ZeroHash, present=false (verify skips it).
function toStep(el) {
  return [
    el.sibling === null ? ethers.ZeroHash : "0x" + el.sibling,
    Boolean(el.siblingIsLeft),
    el.sibling !== null,
  ];
}

function toSteps(proof) {
  return proof.map(toStep);
}

// recipient chunk j (0..9) = 0x10000 | uint16(ethRecipient >> (8*(18-2*j))) -> the sentinel'd big-endian
// 16-bit chunk ethRecipient[2j:2j+2]. (SPEC §7 step 4 / bridge_vault per-lock slot layout: every chunk carries
// the 0x10000 sentinel so a zero chunk is never dropped as a deletion.) Verified against the oracle slot values.
function recipientChunk(ethRecipient, j) {
  return 0x10000n | ((BigInt(ethRecipient) >> BigInt(8 * (18 - 2 * j))) & 0xffffn);
}

// Flip the low byte of a 0x-prefixed bytes32 hex string (for sibling-tamper tests).
function flipByte(b32) {
  const body = b32.slice(2);
  const last = (parseInt(body.slice(-2), 16) ^ 0xff).toString(16).padStart(2, "0");
  return "0x" + body.slice(0, -2) + last;
}

module.exports = { vectors, vaultAddressBytes, toStep, toSteps, recipientChunk, flipByte };
