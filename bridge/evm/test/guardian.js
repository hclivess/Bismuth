"use strict";
// SPEC §10.4 — GuardianSetVerifier (interim honest M-of-N). 2-of-3 passes; 1 sig fails; a guardian
// signing twice is not double-counted; signatures over the wrong root fail.
//
// attestation = abi.encode(bytes[] sigs); each sig is an EIP-191 (toEthSignedMessageHash) signature over
// keccak256(abi.encode(block.chainid, address(this), stateRoot, height)). SPEC §8.
// Assumed constructor: (address[] guardians, uint256 threshold, address governance).

const { expect } = require("chai");
const { ethers } = require("hardhat");

const STATE_ROOT = "0x" + "11".repeat(32);
const HEIGHT = 4242n;

describe("GuardianSetVerifier (interim honest 2-of-3)", function () {
  let verifier, guardians, chainId;

  beforeEach(async function () {
    const [deployer] = await ethers.getSigners();
    // Local wallets: their addresses are the guardian set; they sign purely off-chain (view-only verify).
    guardians = [ethers.Wallet.createRandom(), ethers.Wallet.createRandom(), ethers.Wallet.createRandom()];
    const set = guardians.map((w) => w.address);

    verifier = await (await ethers.getContractFactory("GuardianSetVerifier")).deploy(
      set,
      2,
      deployer.address
    );
    await verifier.waitForDeployment();
    chainId = (await ethers.provider.getNetwork()).chainId;
  });

  // EIP-191 signature by `wallet` over the digest the contract recomputes for `root`.
  async function signRoot(wallet, root) {
    const digest = ethers.keccak256(
      ethers.AbiCoder.defaultAbiCoder().encode(
        ["uint256", "address", "bytes32", "uint64"],
        [chainId, verifier.target, root, HEIGHT]
      )
    );
    return await wallet.signMessage(ethers.getBytes(digest));
  }

  function attest(sigs) {
    return ethers.AbiCoder.defaultAbiCoder().encode(["bytes[]"], [sigs]);
  }

  it("passes with 2 distinct guardian signatures (>= threshold)", async function () {
    const att = attest([await signRoot(guardians[0], STATE_ROOT), await signRoot(guardians[1], STATE_ROOT)]);
    expect(await verifier.verifyStateRoot(STATE_ROOT, HEIGHT, att)).to.equal(true);
  });

  it("fails with only 1 signature (< threshold)", async function () {
    const att = attest([await signRoot(guardians[0], STATE_ROOT)]);
    expect(await verifier.verifyStateRoot(STATE_ROOT, HEIGHT, att)).to.equal(false);
  });

  it("does not double-count the same guardian signing twice", async function () {
    const sig = await signRoot(guardians[0], STATE_ROOT);
    const att = attest([sig, sig]);
    expect(await verifier.verifyStateRoot(STATE_ROOT, HEIGHT, att)).to.equal(false);
  });

  it("fails when signatures are over the wrong root", async function () {
    const wrongRoot = "0x" + "22".repeat(32);
    // Two valid guardian sigs, but over a different root than the one queried -> recovered signers
    // won't match the (chainid,this,STATE_ROOT,height) digest -> not in set -> count 0.
    const att = attest([await signRoot(guardians[0], wrongRoot), await signRoot(guardians[1], wrongRoot)]);
    expect(await verifier.verifyStateRoot(STATE_ROOT, HEIGHT, att)).to.equal(false);
  });
});
