"use strict";
// SPEC §10.1 — Blake2b.sol (EIP-152 precompile 0x09) must reproduce hashlib.blake2b(digest_size=32)
// for every KAT in fixtures/vectors.json.blake2bKats.

const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture } = require("@nomicfoundation/hardhat-network-helpers");
const { vectors } = require("./helpers");

describe("Blake2b (EIP-152 precompile vs hashlib.blake2b)", function () {
  async function deploy() {
    const harness = await (await ethers.getContractFactory("Blake2bHarness")).deploy();
    await harness.waitForDeployment();
    return harness;
  }

  it("reproduces every blake2b-256 KAT", async function () {
    const harness = await loadFixture(deploy);
    expect(vectors.blake2bKats.length).to.be.greaterThan(0);
    for (const kat of vectors.blake2bKats) {
      const input = "0x" + kat.input; // "" -> "0x" (empty input is a single padded final block)
      const got = await harness.h(input);
      expect(got, `KAT '${kat.label}'`).to.equal("0x" + kat.blake2b256);
    }
  });
});
