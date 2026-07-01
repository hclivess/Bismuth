// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IBismuthConsensusVerifier} from "../interfaces/IBismuthConsensusVerifier.sol";
import {IGroth16Verifier} from "../interfaces/IGroth16Verifier.sol";

/// @title ZkBismuthVerifier
/// @notice The ENDGAME consensus verifier for the wBIS bridge (doc/45 §8, Stage 3).
///         It accepts a state root iff a Groth16 proof attests, under Bismuth's own
///         consensus rules, that the root is committed by a finalized Bismuth chain —
///         i.e. a valid PoW header chain with sufficient cumulative work whose tip's VM
///         state commitment equals `stateRoot`. No federation, no honest-majority
///         assumption: trust reduces to the two chains' consensus plus this circuit.
/// @dev    The `IGroth16Verifier` is the snarkjs/gnark-generated verifying-key contract
///         for the Stage-3 circuit. That circuit and its verifying key are NOT in this
///         repo (frontier, doc/45 §3 Stage 3); this adapter is the clearly-marked
///         DROP-IN SEAM. Deploy the generated verifier, then point a new
///         `ZkBismuthVerifier` at it and migrate the bridge via its timelocked
///         `setVerifier` — no wBIS-ownership migration is required.
///
///         The public-signal encoding (`[uint256(stateRoot), uint256(height)]`, in this
///         exact order) is the contract between this seam and the circuit; the circuit
///         author MUST mirror it. A standard generated verifier reverts if a public
///         signal is >= the BN254 scalar field order, so the circuit is responsible for
///         range-reducing / representing the 256-bit root within its field encoding.
contract ZkBismuthVerifier is IBismuthConsensusVerifier {
    /// @notice The generated Groth16 verifying-key contract for the Stage-3 circuit.
    IGroth16Verifier public immutable groth;

    /// @param groth_ the deployed snarkjs/gnark Groth16 verifier for the Bismuth
    ///        PoW-chain + cumulative-work + state-root circuit.
    constructor(IGroth16Verifier groth_) {
        groth = groth_;
    }

    /// @inheritdoc IBismuthConsensusVerifier
    /// @dev `attestation = abi.encode(uint256[2] a, uint256[2][2] b, uint256[2] c)` — a
    ///      Groth16 proof. Forwards to `groth.verifyProof` with public signals
    ///      `[uint256(stateRoot), uint256(height)]`. Returns the verifier's verdict; the
    ///      proof itself is what establishes finality, so this needs no extra trust knob.
    function verifyStateRoot(bytes32 stateRoot, uint64 height, bytes calldata attestation)
        external
        view
        override
        returns (bool)
    {
        (uint256[2] memory a, uint256[2][2] memory b, uint256[2] memory c) =
            abi.decode(attestation, (uint256[2], uint256[2][2], uint256[2]));

        // Public signals as a FIXED-size array, matching a stock snarkjs/gnark verifier's
        // `uint256[N]` (N == 2 here); a dynamic `uint256[]` would have a different selector and
        // miss the generated function entirely (SPEC §8 / IGroth16Verifier).
        uint256[2] memory input;
        input[0] = uint256(stateRoot);
        input[1] = uint256(height);
        return groth.verifyProof(a, b, c, input);
    }
}
