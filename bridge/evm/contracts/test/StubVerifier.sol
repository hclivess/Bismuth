// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IBismuthConsensusVerifier} from "../interfaces/IBismuthConsensusVerifier.sol";

/// @notice Test-only consensus verifier whose result is a settable bool, so BismuthBridge can be
///         exercised independently of the real GuardianSet/zk attestation logic. NOT for production.
contract StubVerifier is IBismuthConsensusVerifier {
    bool public result = true;

    function setResult(bool r) external {
        result = r;
    }

    function verifyStateRoot(bytes32, uint64, bytes calldata)
        external
        view
        override
        returns (bool)
    {
        return result;
    }
}
