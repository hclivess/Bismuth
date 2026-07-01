// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title IGroth16Verifier — the snarkjs/gnark-generated Groth16 verifier shape (doc/45, SPEC §8)
/// @notice Standard verifying-contract surface produced by snarkjs (`Verifier.sol`) and gnark
///         (`verifier.sol`) for a Groth16 proving system over the BN254 (alt_bn128) curve.
///         `ZkBismuthVerifier` is the drop-in adapter that decodes a bridge attestation into
///         `(a, b, c)` and calls `verifyProof` with the public inputs `[uint256(stateRoot), height]`.
/// @dev The concrete verifying key / circuit is the Stage-3 frontier (proves the Bismuth PoW header
///      chain + cumulative work + the committed VM state root) and is NOT in-repo; this interface is
///      the stable seam against which the generated contract is wired.
///
///      Argument shape notes (matching the generated code exactly):
///        - `a`, `c` are G1 points encoded as `[x, y]`.
///        - `b` is a G2 point encoded as `[[x_c1, x_c0], [y_c1, y_c0]]` (the snarkjs/gnark field order).
///        - `input` is the public-signal vector. A stock snarkjs/gnark verifier declares this as a
///          FIXED-size `uint256[N]` array (N == the circuit's public-input count), NOT a dynamic
///          `uint256[]`. The two have DIFFERENT function selectors, so the seam MUST use the fixed
///          shape or the external call would hit no function and revert. The Stage-3 circuit commits
///          exactly two public signals — `[uint256(stateRoot), uint256(height)]` (SPEC §8) — so N == 2.
///      All coordinates/signals are field elements < the BN254 scalar field; the verifier reverts (or
///      returns false in some generators) on malformed/out-of-field inputs.
interface IGroth16Verifier {
    /// @notice Verify a Groth16 proof against the embedded verifying key and public inputs.
    /// @param a     G1 point [x, y].
    /// @param b     G2 point [[x_c1, x_c0], [y_c1, y_c0]].
    /// @param c     G1 point [x, y].
    /// @param input public input signals, fixed at the circuit's 2 signals `[uint256(stateRoot), uint256(height)]`.
    /// @return ok true iff the proof is valid for `input` under the verifying key.
    function verifyProof(
        uint256[2] calldata a,
        uint256[2][2] calldata b,
        uint256[2] calldata c,
        uint256[2] calldata input
    ) external view returns (bool ok);
}
