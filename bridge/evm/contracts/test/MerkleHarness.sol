// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BismuthMerkle} from "../lib/BismuthMerkle.sol";

/// @notice Test-only harness exposing the internal BismuthMerkle library functions to JS so the
///         oracle vectors (EMPTY_ROOT, per-slot leafHash, inclusion proofs) can be asserted.
///         `verify` takes the same `Step{bytes32 sibling; bool isLeft; bool present;}` the bridge uses.
///         NOT part of the deployed bridge.
contract MerkleHarness {
    function emptyRoot() external view returns (bytes32) {
        return BismuthMerkle.EMPTY_ROOT;
    }

    function leafHash(bytes memory preimage) external view returns (bytes32) {
        return BismuthMerkle.leafHash(preimage);
    }

    function nodeHash(bytes32 left, bytes32 right) external view returns (bytes32) {
        return BismuthMerkle.nodeHash(left, right);
    }

    function verify(bytes32 root, bytes32 leaf, BismuthMerkle.Step[] memory proof)
        external
        view
        returns (bool)
    {
        return BismuthMerkle.verify(root, leaf, proof);
    }
}
