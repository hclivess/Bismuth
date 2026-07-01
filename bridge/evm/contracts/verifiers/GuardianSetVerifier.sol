// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IBismuthConsensusVerifier} from "../interfaces/IBismuthConsensusVerifier.sol";

/// @title GuardianSetVerifier
/// @notice INTERIM consensus verifier for the wBIS bridge (doc/45 §8). This is an
///         **honest-majority federation** — an M-of-N guardian set attests that a
///         given Bismuth VM state root is FINALIZED. It is deployable today and is the
///         documented *interim* trust assumption, **NOT the endgame**. The endgame is
///         `ZkBismuthVerifier`, swapped in via the bridge's timelocked `setVerifier`
///         with no wBIS-ownership migration.
/// @dev    Trust model: the bridge is trustless *except* for `IBismuthConsensusVerifier`.
///         While this verifier is installed, a colluding majority of the guardian set
///         (≥ `threshold`) could attest to a root Bismuth never finalized and thus mint
///         unbacked wBIS. That is the price of going live before the zk circuit exists.
///         Guardians' SOLE duty is to sign roots that Bismuth consensus has FINALIZED.
///
///         The guardian set and threshold are mutated only by `governance` (expected to
///         itself be a multisig/timelock) and every mutation is additionally
///         time-locked here (`CHANGE_DELAY`) so a compromised governance key cannot
///         instantly rewrite the trust set and mint infinitely. `pause`-like emergency
///         powers live on the bridge, not here.
contract GuardianSetVerifier is IBismuthConsensusVerifier {
    // -------------------------------------------------------------------------
    // Guardian set
    // -------------------------------------------------------------------------

    /// @notice Whether `account` is an active guardian.
    mapping(address => bool) public isGuardian;

    /// @notice Number of active guardians (N).
    uint256 public guardianCount;

    /// @notice Minimum number of DISTINCT guardian signatures required (M of N).
    uint256 public threshold;

    /// @notice The timelocked administrator of the guardian set / threshold.
    address public governance;

    // -------------------------------------------------------------------------
    // Timelock
    // -------------------------------------------------------------------------

    /// @notice Delay every set/threshold/governance change must mature for.
    uint256 public constant CHANGE_DELAY = 2 days;

    /// @notice Kinds of timelocked actions.
    enum ActionType {
        AddGuardian,
        RemoveGuardian,
        SetThreshold,
        TransferGovernance
    }

    /// @notice Earliest-execution timestamp for a queued action id (0 = not queued).
    mapping(bytes32 => uint256) public etaOf;

    // -------------------------------------------------------------------------
    // Events
    // -------------------------------------------------------------------------

    event ActionQueued(bytes32 indexed id, ActionType indexed kind, uint256 eta);
    event ActionCancelled(bytes32 indexed id);
    event GuardianAdded(address indexed guardian, uint256 guardianCount);
    event GuardianRemoved(address indexed guardian, uint256 guardianCount);
    event ThresholdChanged(uint256 oldThreshold, uint256 newThreshold);
    event GovernanceTransferred(address indexed oldGovernance, address indexed newGovernance);

    // -------------------------------------------------------------------------
    // Errors
    // -------------------------------------------------------------------------

    error NotGovernance();
    error ZeroAddress();
    error InvalidThreshold();
    error AlreadyGuardian();
    error NotGuardian();
    error DuplicateGuardian();
    error AlreadyQueued();
    error NotQueued();
    error TimelockNotElapsed();
    error WouldStrandThreshold();

    modifier onlyGovernance() {
        if (msg.sender != governance) revert NotGovernance();
        _;
    }

    /// @param initialGuardians distinct, non-zero guardian addresses (N).
    /// @param initialThreshold M, with 1 <= M <= N.
    /// @param governance_      the timelocked admin (a multisig/timelock).
    constructor(address[] memory initialGuardians, uint256 initialThreshold, address governance_) {
        if (governance_ == address(0)) revert ZeroAddress();
        uint256 n = initialGuardians.length;
        if (initialThreshold < 1 || initialThreshold > n) revert InvalidThreshold();
        for (uint256 i = 0; i < n; ++i) {
            address g = initialGuardians[i];
            if (g == address(0)) revert ZeroAddress();
            if (isGuardian[g]) revert DuplicateGuardian();
            isGuardian[g] = true;
            emit GuardianAdded(g, i + 1);
        }
        guardianCount = n;
        threshold = initialThreshold;
        governance = governance_;
        emit ThresholdChanged(0, initialThreshold);
        emit GovernanceTransferred(address(0), governance_);
    }

    // -------------------------------------------------------------------------
    // Verification (IBismuthConsensusVerifier)
    // -------------------------------------------------------------------------

    /// @notice The inner message guardians sign (before the EIP-191 personal-sign
    ///         prefix is applied). Bound to `block.chainid` and `address(this)` so a
    ///         signature is replay-proof across chains and across verifier instances.
    /// @dev    Guardians produce `eth_sign`/`personal_sign` over this 32-byte hash; the
    ///         on-chain check re-applies the `\x19Ethereum Signed Message:\n32` prefix.
    function messageHash(bytes32 stateRoot, uint64 height) public view returns (bytes32) {
        return keccak256(abi.encode(block.chainid, address(this), stateRoot, height));
    }

    /// @inheritdoc IBismuthConsensusVerifier
    /// @dev `attestation = abi.encode(bytes[] sigs)`. Each sig is an EIP-191 signature
    ///      over `messageHash(stateRoot, height)`. Counts DISTINCT guardian signers:
    ///      non-guardian, zero-address (failed-recover), and duplicate signers are
    ///      ignored (never double-counted). Returns true iff the distinct guardian count
    ///      reaches `threshold`. This is a pure attestation count; whether the root is
    ///      truly FINALIZED is the guardians' off-chain duty.
    function verifyStateRoot(bytes32 stateRoot, uint64 height, bytes calldata attestation)
        external
        view
        override
        returns (bool)
    {
        bytes[] memory sigs = abi.decode(attestation, (bytes[]));
        uint256 n = sigs.length;
        if (n == 0) return false;

        bytes32 ethMsg = _toEthSignedMessageHash(messageHash(stateRoot, height));

        address[] memory counted = new address[](n);
        uint256 count = 0;
        uint256 thr = threshold;

        for (uint256 i = 0; i < n; ++i) {
            address signer = _recover(ethMsg, sigs[i]);
            if (signer == address(0) || !isGuardian[signer]) continue;

            // Dedup against guardians already counted (seen-set; N is small).
            bool seen = false;
            for (uint256 j = 0; j < count; ++j) {
                if (counted[j] == signer) {
                    seen = true;
                    break;
                }
            }
            if (seen) continue;

            counted[count] = signer;
            unchecked {
                ++count;
            }
            if (count >= thr) return true; // early-out once quorum is reached
        }
        return count >= thr;
    }

    // -------------------------------------------------------------------------
    // Governance: queue → mature → execute (all set/threshold changes timelocked)
    // -------------------------------------------------------------------------

    function queueAddGuardian(address guardian) external onlyGovernance {
        if (guardian == address(0)) revert ZeroAddress();
        if (isGuardian[guardian]) revert AlreadyGuardian();
        _queue(_id(ActionType.AddGuardian, uint256(uint160(guardian))), ActionType.AddGuardian);
    }

    function executeAddGuardian(address guardian) external onlyGovernance {
        _consume(_id(ActionType.AddGuardian, uint256(uint160(guardian))));
        if (guardian == address(0)) revert ZeroAddress();
        if (isGuardian[guardian]) revert AlreadyGuardian();
        isGuardian[guardian] = true;
        uint256 c = guardianCount + 1;
        guardianCount = c;
        emit GuardianAdded(guardian, c);
    }

    function queueRemoveGuardian(address guardian) external onlyGovernance {
        if (!isGuardian[guardian]) revert NotGuardian();
        _queue(_id(ActionType.RemoveGuardian, uint256(uint160(guardian))), ActionType.RemoveGuardian);
    }

    function executeRemoveGuardian(address guardian) external onlyGovernance {
        _consume(_id(ActionType.RemoveGuardian, uint256(uint160(guardian))));
        if (!isGuardian[guardian]) revert NotGuardian();
        // Fail-closed safety rail: never let removal strand the threshold above N.
        if (guardianCount - 1 < threshold) revert WouldStrandThreshold();
        isGuardian[guardian] = false;
        uint256 c = guardianCount - 1;
        guardianCount = c;
        emit GuardianRemoved(guardian, c);
    }

    function queueSetThreshold(uint256 newThreshold) external onlyGovernance {
        if (newThreshold < 1) revert InvalidThreshold();
        _queue(_id(ActionType.SetThreshold, newThreshold), ActionType.SetThreshold);
    }

    function executeSetThreshold(uint256 newThreshold) external onlyGovernance {
        _consume(_id(ActionType.SetThreshold, newThreshold));
        if (newThreshold < 1 || newThreshold > guardianCount) revert InvalidThreshold();
        uint256 old = threshold;
        threshold = newThreshold;
        emit ThresholdChanged(old, newThreshold);
    }

    function queueTransferGovernance(address newGovernance) external onlyGovernance {
        if (newGovernance == address(0)) revert ZeroAddress();
        _queue(_id(ActionType.TransferGovernance, uint256(uint160(newGovernance))), ActionType.TransferGovernance);
    }

    function executeTransferGovernance(address newGovernance) external onlyGovernance {
        _consume(_id(ActionType.TransferGovernance, uint256(uint160(newGovernance))));
        if (newGovernance == address(0)) revert ZeroAddress();
        address old = governance;
        governance = newGovernance;
        emit GovernanceTransferred(old, newGovernance);
    }

    /// @notice Cancel any queued action before it matures.
    function cancelAction(bytes32 id) external onlyGovernance {
        if (etaOf[id] == 0) revert NotQueued();
        delete etaOf[id];
        emit ActionCancelled(id);
    }

    // -------------------------------------------------------------------------
    // Internal: timelock helpers
    // -------------------------------------------------------------------------

    function _id(ActionType kind, uint256 param) internal pure returns (bytes32) {
        return keccak256(abi.encode(kind, param));
    }

    function _queue(bytes32 id, ActionType kind) internal {
        if (etaOf[id] != 0) revert AlreadyQueued();
        uint256 eta = block.timestamp + CHANGE_DELAY;
        etaOf[id] = eta;
        emit ActionQueued(id, kind, eta);
    }

    function _consume(bytes32 id) internal {
        uint256 eta = etaOf[id];
        if (eta == 0) revert NotQueued();
        if (block.timestamp < eta) revert TimelockNotElapsed();
        delete etaOf[id];
    }

    // -------------------------------------------------------------------------
    // Internal: minimal inline ECDSA (no OpenZeppelin in repo)
    // -------------------------------------------------------------------------

    /// @dev Re-applies the EIP-191 `personal_sign` prefix for a 32-byte payload.
    function _toEthSignedMessageHash(bytes32 hash) internal pure returns (bytes32) {
        return keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", hash));
    }

    /// @dev Recovers the signer of `hash` from a 65-byte `(r,s,v)` signature. Returns
    ///      `address(0)` for malformed length, non-{27,28} `v`, or a malleable high-`s`
    ///      (EIP-2). A zero return is simply not a guardian, so it is ignored upstream.
    function _recover(bytes32 hash, bytes memory signature) internal pure returns (address) {
        if (signature.length != 65) return address(0);

        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := mload(add(signature, 0x20))
            s := mload(add(signature, 0x40))
            v := byte(0, mload(add(signature, 0x60)))
        }

        // EIP-2: reject the upper half of the curve order (signature malleability).
        if (uint256(s) > 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0) {
            return address(0);
        }
        if (v != 27 && v != 28) return address(0);

        return ecrecover(hash, v, r, s);
    }
}
