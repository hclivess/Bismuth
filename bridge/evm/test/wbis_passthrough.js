"use strict";
// Finding #3 — once the bridge owns wBIS, the token's onlyOwner pause()/unpause() are only reachable
// through the owner. The bridge forwards them via governance-gated pauseWbis()/unpauseWbis(); without
// these the live token's Pausable emergency brake would be silently lost on ownership transfer.

const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture } = require("@nomicfoundation/hardhat-network-helpers");
const { vectors, vaultAddressBytes } = require("./helpers");

const VAULT_BYTES = vaultAddressBytes(vectors.vaultAddress);

describe("BismuthBridge wBIS pause passthrough", function () {
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

    // Hand the token's owner (mint AND pause authority) to the bridge — the non-custodial peg.
    await (await wbis.transferOwnership(bridge.target)).wait();
    return { deployer, governance, other, wbis, bridge };
  }

  it("governance can pause/unpause the wBIS token through the bridge owner", async function () {
    const { wbis, bridge, governance } = await loadFixture(deploy);
    expect(await wbis.paused()).to.equal(false);

    await bridge.connect(governance).pauseWbis();
    expect(await wbis.paused()).to.equal(true);

    await bridge.connect(governance).unpauseWbis();
    expect(await wbis.paused()).to.equal(false);
  });

  it("a paused wBIS token blocks transfers (the brake actually bites)", async function () {
    const { wbis, bridge, governance, other } = await loadFixture(deploy);
    await bridge.connect(governance).pauseWbis();
    // Any transfer must revert while the token is paused (PausableToken.whenNotPaused).
    await expect(wbis.connect(other).transfer(other.address, 0)).to.be.reverted;
  });

  it("non-governance cannot drive the token pause passthrough", async function () {
    const { bridge, other } = await loadFixture(deploy);
    await expect(bridge.connect(other).pauseWbis()).to.be.revertedWith("not governance");
    await expect(bridge.connect(other).unpauseWbis()).to.be.revertedWith("not governance");
  });
});
