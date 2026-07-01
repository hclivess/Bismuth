require("@nomicfoundation/hardhat-toolbox");

/**
 * Bismuth wBIS bridge — Hardhat config.
 * EVM target: a hardfork with the EIP-152 BLAKE2 F precompile (0x09), i.e. Istanbul or later.
 * The default Hardhat network uses a recent hardfork, so 0x09 is available for Blake2b.sol.
 * viaIR + optimizer keep the blake2b compression function under the stack limit.
 */
module.exports = {
  solidity: {
    version: "0.8.24",
    settings: {
      optimizer: { enabled: true, runs: 200 },
      viaIR: true,
      evmVersion: "cancun",
    },
  },
  networks: {
    hardhat: { hardfork: "cancun", allowUnlimitedContractSize: false },
  },
  mocha: { timeout: 120000 },
};
