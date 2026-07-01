// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Blake2b} from "./Blake2b.sol";

/// @title BismuthMerkle
/// @notice Solidity mirror of `vm_merkle.py` (doc/45 §4). The Bismuth VM state
///         commitment is a BLAKE2b-256 binary Merkle tree where a lone node at a
///         level is PROMOTED unchanged (carried up, never duplicated — CVE-2012-2459
///         safe). Proofs carry one step per level from leaf to root; a "promoted"
///         level has no sibling. Verification needs no tree size — it matches
///         `vm_merkle.verify_proof` byte-for-byte.
library BismuthMerkle {
    /// @dev Domain-separation tags hashed in front of leaf/node preimages.
    bytes1 internal constant LEAF_TAG = 0x00;
    bytes1 internal constant NODE_TAG = 0x01;

    /// @dev EMPTY_ROOT = blake2b("bismuth-vm-merkle/empty-state"). Copied EXACTLY
    ///      from fixtures/vectors.json.emptyRoot (the cross-chain source of truth).
    bytes32 internal constant EMPTY_ROOT =
        0x83ff5f30269c0c043304bbcd6fc6a97c23cb4ee912238a05899d31b85c9b4805;

    /// @notice One level of a Merkle inclusion proof, leaf→root.
    /// @param sibling  the sibling node hash at this level (ignored when !present).
    /// @param isLeft   true if the sibling is the LEFT child (node(sibling, h)),
    ///                 false if it is the RIGHT child (node(h, sibling)).
    /// @param present  false ⇔ a vm_merkle promoted level (no sibling; carry up).
    struct Step {
        bytes32 sibling;
        bool isLeft;
        bool present;
    }

    /// @notice leafHash = blake2b(LEAF_TAG || preimage).
    function leafHash(bytes memory preimage) internal view returns (bytes32) {
        return Blake2b.hash256(abi.encodePacked(LEAF_TAG, preimage));
    }

    /// @notice nodeHash = blake2b(NODE_TAG || left || right).
    function nodeHash(bytes32 l, bytes32 r) internal view returns (bytes32) {
        return Blake2b.hash256(abi.encodePacked(NODE_TAG, l, r));
    }

    /// @notice Verify a leaf hash against `root` using a leaf→root proof.
    /// @dev Mirrors vm_merkle.verify_proof: start at the leaf, fold each present
    ///      step (promoted levels are skipped), and compare to the root. No tree
    ///      size is required.
    function verify(bytes32 root, bytes32 leaf, Step[] memory proof)
        internal
        view
        returns (bool)
    {
        bytes32 h = leaf;
        uint256 n = proof.length;
        for (uint256 i = 0; i < n; ++i) {
            Step memory step = proof[i];
            if (!step.present) {
                continue;
            }
            h = step.isLeft
                ? nodeHash(step.sibling, h)
                : nodeHash(h, step.sibling);
        }
        return h == root;
    }
}
