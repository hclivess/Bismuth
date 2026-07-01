// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title IBismuthConsensusVerifier — the single pluggable trust root of the wBIS bridge (doc/45, SPEC §7/§8/§9)
/// @notice Abstracts "is this `stateRoot` a real, FINALIZED Bismuth VM state root at `height`?" behind one
///         call. It is the ONLY trust assumption on the bridge's value path: everything else (BLAKE2b/Merkle
///         inclusion, the `_trans` nullifier, units, pause, timelock) is trustless and final on-chain.
/// @dev Implementations ship along a trust spectrum and are swapped via the bridge's timelocked `setVerifier`:
///        - `GuardianSetVerifier` — interim honest M-of-N federation (deployable today).
///        - `ZkBismuthVerifier`   — endgame zk adapter over a Groth16 proof of Bismuth PoW + cumulative work.
///
///      CRITICAL FINALITY REQUIREMENT — `verifyStateRoot` MUST return `true` ONLY for a state root that
///      Bismuth consensus has FINALIZED. A root from an un-finalized / reorg-able / merely-tip block MUST
///      return `false`. Returning `true` for a non-final root is a critical bug: it would let an attacker
///      mint wBIS against a Bismuth state that is later reorged away, breaking the 1:1 peg. Enforcing
///      finality (sufficient confirmation depth, cumulative-work / guardian-attested finality, etc.) is
///      entirely the implementation's responsibility — callers treat a `true` result as economically final.
///
///      This function MUST be side-effect free (`view`): it is a pure predicate over (root, height,
///      attestation) and the implementation's own state (guardian set, verifying key, ...).
interface IBismuthConsensusVerifier {
    /// @notice Verify that `stateRoot` is a finalized Bismuth VM state root at block `height`.
    /// @param stateRoot   the claimed Bismuth VM Merkle state root (BLAKE2b-256, doc/45 §4).
    /// @param height      the Bismuth block height the root is committed at.
    /// @param attestation implementation-specific proof of finality (e.g. encoded guardian signatures,
    ///                    or an encoded Groth16 proof) binding `stateRoot` at `height` to consensus.
    /// @return ok true iff `stateRoot` is a FINALIZED Bismuth state root at `height`; false otherwise.
    function verifyStateRoot(
        bytes32 stateRoot,
        uint64 height,
        bytes calldata attestation
    ) external view returns (bool ok);
}
