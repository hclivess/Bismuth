// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Blake2b} from "../lib/Blake2b.sol";

/// @notice Test-only harness exposing the internal `Blake2b.hash256` to JS so every
///         `fixtures/vectors.json.blake2bKats` entry can be asserted against the EIP-152
///         (precompile 0x09) implementation. NOT part of the deployed bridge.
contract Blake2bHarness {
    function h(bytes memory input) external view returns (bytes32) {
        return Blake2b.hash256(input);
    }
}
