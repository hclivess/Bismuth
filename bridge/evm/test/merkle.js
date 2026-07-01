"use strict";
// SPEC §10.2 — BismuthMerkle: EMPTY_ROOT constant, per-slot leafHash, and inclusion-proof verification
// must match the consensus impl (vm_merkle.py) as captured in fixtures/vectors.json. Tamper cases fail.

const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture } = require("@nomicfoundation/hardhat-network-helpers");
const { vectors, toSteps, flipByte } = require("./helpers");

const STATE_ROOT = "0x" + vectors.stateRoot;

describe("BismuthMerkle (blake2b binary Merkle, vm_merkle.py)", function () {
  async function deploy() {
    const harness = await (await ethers.getContractFactory("MerkleHarness")).deploy();
    await harness.waitForDeployment();
    return harness;
  }

  it("EMPTY_ROOT matches the oracle", async function () {
    const harness = await loadFixture(deploy);
    expect(await harness.emptyRoot()).to.equal("0x" + vectors.emptyRoot);
  });

  it("leafHash + verify every lock slot against the state root", async function () {
    const harness = await loadFixture(deploy);
    for (const lock of vectors.locks) {
      for (const slot of lock.slots) {
        const leaf = await harness.leafHash("0x" + slot.leafPreimage);
        expect(leaf, `lock ${lock.lockId} slot ${slot.slot} leafHash`).to.equal("0x" + slot.leafHash);
        const ok = await harness.verify(STATE_ROOT, leaf, toSteps(slot.proof));
        expect(ok, `lock ${lock.lockId} slot ${slot.slot} verify`).to.equal(true);
      }
    }
  });

  it("rejects a tampered sibling, a flipped isLeft, and a dropped present step", async function () {
    const harness = await loadFixture(deploy);
    const slot = vectors.locks[0].slots[0];
    const leaf = "0x" + slot.leafHash;
    const steps = toSteps(slot.proof);

    // sanity: the untampered proof verifies
    expect(await harness.verify(STATE_ROOT, leaf, steps)).to.equal(true);

    // 1) flip a byte of the first sibling -> wrong root
    const flippedSibling = steps.map((s) => [...s]);
    flippedSibling[0][0] = flipByte(flippedSibling[0][0]);
    expect(await harness.verify(STATE_ROOT, leaf, flippedSibling)).to.equal(false);

    // 2) flip isLeft on the first step -> hashing order changes -> wrong root
    const flippedSide = steps.map((s) => [...s]);
    flippedSide[0][1] = !flippedSide[0][1];
    expect(await harness.verify(STATE_ROOT, leaf, flippedSide)).to.equal(false);

    // 3) drop a present step -> incomplete path -> wrong root
    const dropped = steps.slice(1);
    expect(await harness.verify(STATE_ROOT, leaf, dropped)).to.equal(false);
  });
});
