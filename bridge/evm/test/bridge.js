"use strict";
// SPEC §10.3 — BismuthBridge.pegInMint end-to-end against MockWBIS + a settable StubVerifier.
// Each oracle lock mints `amount` wBIS to `ethRecipient` and emits PegInMinted; replay / bad-amount /
// lockId=0 / amount>MAX_LOCK / paused / verifier=false all revert.
//
// Assumed BismuthBridge constructor (per SPEC §7 state): (IWBIS wbis, IBismuthConsensusVerifier verifier,
// bytes vaultAddress, address governance). vaultAddress is the 56 ASCII bytes of the hex string.

const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture } = require("@nomicfoundation/hardhat-network-helpers");
const { vectors, vaultAddressBytes, toSteps, recipientChunk } = require("./helpers");

const VAULT_BYTES = vaultAddressBytes(vectors.vaultAddress);
const STATE_ROOT = "0x" + vectors.stateRoot;
const HEIGHT = 100n;
const ATTESTATION = "0x"; // StubVerifier ignores it
const MAX_LOCK = (1n << 32n) - 1n; // type(uint32).max

describe("BismuthBridge.pegInMint", function () {
  async function deploy() {
    const [deployer, governance, other] = await ethers.getSigners();

    const wbis = await (await ethers.getContractFactory("MockWBIS")).deploy();
    await wbis.waitForDeployment();

    const verifier = await (await ethers.getContractFactory("StubVerifier")).deploy();
    await verifier.waitForDeployment();

    const bridge = await (await ethers.getContractFactory("BismuthBridge")).deploy(
      wbis.target,
      verifier.target,
      VAULT_BYTES,
      governance.address
    );
    await bridge.waitForDeployment();

    // Hand the wBIS mint authority (its owner) to the bridge: the non-custodial peg.
    await (await wbis.transferOwnership(bridge.target)).wait();

    return { deployer, governance, other, wbis, verifier, bridge };
  }

  // Build the pegInMint argument list from an oracle lock, allowing field overrides for negative tests.
  function pegArgs(lock, overrides = {}) {
    const amount = overrides.amount !== undefined ? BigInt(overrides.amount) : BigInt(lock.amount);
    const lockId = overrides.lockId !== undefined ? BigInt(overrides.lockId) : BigInt(lock.lockId);
    const amountProof = toSteps(lock.slots[0].proof); // slot lockId*16
    const recipientProofs = Array.from({ length: 10 }, (_, j) => toSteps(lock.slots[1 + j].proof)); // slots lockId*16+1+j
    return [HEIGHT, STATE_ROOT, ATTESTATION, lockId, amount, lock.ethRecipient, amountProof, recipientProofs];
  }

  // trans nullifier = keccak256(abi.encodePacked("BIS-PEGIN:", vaultAddress, lockId)) — SPEC §7.3.
  function transFor(lockId) {
    return ethers.keccak256(
      ethers.solidityPacked(["string", "bytes", "uint256"], ["BIS-PEGIN:", VAULT_BYTES, BigInt(lockId)])
    );
  }

  it("recipient-chunk shift + sentinel reproduces the oracle slot values", function () {
    for (const lock of vectors.locks) {
      for (let j = 0; j < 10; j++) {
        expect(recipientChunk(lock.ethRecipient, j), `lock ${lock.lockId} chunk ${j}`).to.equal(
          BigInt(lock.slots[1 + j].value)
        );
      }
    }
  });

  for (const lock of vectors.locks) {
    it(`mints lock ${lock.lockId} (${lock.amount} -> ${lock.ethRecipient})`, async function () {
      const { wbis, bridge } = await loadFixture(deploy);
      await expect(bridge.pegInMint(...pegArgs(lock)))
        .to.emit(bridge, "PegInMinted")
        .withArgs(
          BigInt(lock.lockId),
          ethers.getAddress(lock.ethRecipient),
          BigInt(lock.amount),
          transFor(lock.lockId),
          STATE_ROOT,
          HEIGHT
        );
      expect(await wbis.balanceOf(lock.ethRecipient)).to.equal(BigInt(lock.amount));
    });
  }

  it("reverts on replay (bridge nullifier consumed)", async function () {
    const { bridge } = await loadFixture(deploy);
    const lock = vectors.locks[0];
    await bridge.pegInMint(...pegArgs(lock));
    await expect(bridge.pegInMint(...pegArgs(lock))).to.be.reverted;
  });

  it("reverts on a mutated amount (rebuilt leaf no longer in the committed state)", async function () {
    const { bridge } = await loadFixture(deploy);
    const lock = vectors.locks[0];
    await expect(bridge.pegInMint(...pegArgs(lock, { amount: BigInt(lock.amount) + 1n }))).to.be.reverted;
  });

  it("reverts when lockId == 0", async function () {
    const { bridge } = await loadFixture(deploy);
    await expect(bridge.pegInMint(...pegArgs(vectors.locks[0], { lockId: 0 }))).to.be.reverted;
  });

  it("reverts when amount > MAX_LOCK", async function () {
    const { bridge } = await loadFixture(deploy);
    await expect(bridge.pegInMint(...pegArgs(vectors.locks[0], { amount: MAX_LOCK + 1n }))).to.be.reverted;
  });

  it("reverts when paused", async function () {
    const { bridge, governance } = await loadFixture(deploy);
    await bridge.connect(governance).pause();
    await expect(bridge.pegInMint(...pegArgs(vectors.locks[0]))).to.be.reverted;
  });

  it("reverts when the verifier returns false", async function () {
    const { bridge, verifier } = await loadFixture(deploy);
    await verifier.setResult(false);
    await expect(bridge.pegInMint(...pegArgs(vectors.locks[0]))).to.be.reverted;
  });
});
