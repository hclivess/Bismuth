// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IWBIS} from "./interfaces/IWBIS.sol";
import {IBismuthConsensusVerifier} from "./interfaces/IBismuthConsensusVerifier.sol";
import {BismuthMerkle} from "./lib/BismuthMerkle.sol";

/// @dev Minimal non-reentrancy guard (OpenZeppelin is not vendored in this Hardhat project, so we inline
///      the well-known pattern rather than add a dependency on the value path).
abstract contract ReentrancyGuard {
    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED = 2;
    uint256 private _status = _NOT_ENTERED;

    modifier nonReentrant() {
        require(_status != _ENTERED, "reentrant");
        _status = _ENTERED;
        _;
        _status = _NOT_ENTERED;
    }
}

/// @title  BismuthBridge — the non-custodial peg between native ("physical") BIS and the deployed wBIS token.
/// @notice doc/45 (wBIS bridge, EVM side). This contract is the mint authority of the already-deployed wBIS
///         ERC-20 (ETH `0xf5cb350b40726b5bcf170d12e162b6193b291b41`, BSC
///         `0x56672ecb506301b1e32ed28552797037c54d36a9`). The live token exposes a bridge-shaped
///         `mint(to, amount, trans)` callable only by its `owner`; the peg is made non-custodial by handing
///         that ownership to THIS contract (see deploy handoff below). From then on wBIS can be minted *only*
///         by `pegInMint`, which proves — fully on-chain — that an equal amount of native BIS was locked in
///         the Bismuth-side vault (`contracts/bridge_vault.py`) and committed under Bismuth's consensus state
///         root. There is NO operator key on the value path.
///
/// @dev    UNITS. Native BIS atomic unit = 1e-8 BIS; wBIS `decimals == 8`. The peg is 1:1 at atomic units: a
///         lock of `A` atomic units mints exactly `A` wBIS base units. No scaling anywhere.
///
/// @dev    TRUST FRAMING — do NOT overclaim. Trustless and final on-chain: the mint/burn wiring, the `trans`
///         nullifier (enforced here AND inside the token's `transactions` map), the BLAKE2b/Merkle inclusion
///         check, the unit mapping, `pause`, and the governance timelock. The ONE pluggable trust assumption
///         is `verifier` (`IBismuthConsensusVerifier`): it alone decides whether a `(stateRoot, height)` pair
///         is a finalized Bismuth root. It ships as `GuardianSetVerifier` (honest M-of-N federation — live
///         today) and upgrades, via the timelocked `setVerifier`, to `ZkBismuthVerifier` (proof of Bismuth's
///         PoW header chain + cumulative work; doc/45's endgame) with NO wBIS-ownership migration. Everything
///         else in this file is unconditional EVM-enforced logic.
///
/// @dev    PEG-OUT is intentionally NOT implemented on this bridge. A wBIS holder redeems back to native BIS
///         by calling `wBIS.burn(amount, bismuthAddrBytes)` DIRECTLY on the token (it emits
///         `Burn(burner, value, addr)`); the Bismuth-side release contract consumes a proof of that `Burn`
///         log. This contract only governs the mint (peg-in) direction.
///
/// @dev    PER-LOCK CAP. `amount <= MAX_LOCK == type(uint32).max` (~42.94 BIS) is a documented limitation of
///         the Bismuth VM's 32-bit callvalue (`vm_engine.py`). Larger transfers use multiple locks until the
///         VM widens callvalue; the cap is NOT a property of the EVM side and lifts automatically once the
///         vault records wider values.
contract BismuthBridge is ReentrancyGuard {
    // ---------------------------------------------------------------------------------------------------------
    // Immutable / config state
    // ---------------------------------------------------------------------------------------------------------

    /// @notice The deployed wBIS token this bridge is (or will become) the owner/mint-authority of.
    IWBIS public immutable wbis;

    /// @notice The single pluggable trust root — only it can attest a `(stateRoot, height)` is a FINALIZED
    ///         Bismuth consensus root. Swappable only through the governance timelock.
    IBismuthConsensusVerifier public verifier;

    /// @notice The Bismuth vault contract address as its 56-byte ASCII string (NOT decoded). This is the `V`
    ///         that prefixes every storage-leaf preimage (§3.1); it pins which Bismuth contract's locks this
    ///         bridge will honor. Constant for the life of the bridge.
    bytes public vaultAddress;

    /// @notice Per-lock nullifier set. `consumed[trans]` is bound ONLY to `(vaultAddress, lockId)` so a given
    ///         lock can be minted at most once, idempotently across any state root/height that contains it.
    mapping(bytes32 => bool) public consumed;

    /// @notice The governance principal (intended: a multisig/timelock). Holds the privileged, timelocked
    ///         knobs and the immediate pause switch. Never on the mint value path.
    address public governance;

    /// @notice Immediate emergency stop for `pegInMint`. Toggled by governance with no delay (safety can't
    ///         wait); it does NOT touch the timelocked trust knobs.
    bool public paused;

    /// @notice Max atomic units per lock — see PER-LOCK CAP note (VM 32-bit callvalue).
    uint256 public constant MAX_LOCK = type(uint32).max;

    /// @notice Mandatory delay before any trust-affecting governance change can execute. A compromised
    ///         governance key therefore cannot instantly swap the verifier (and mint infinitely) or hand off
    ///         wBIS ownership — the community has `CHANGE_DELAY` to react / exit.
    uint256 public constant CHANGE_DELAY = 2 days;

    // ---------------------------------------------------------------------------------------------------------
    // Timelock queues (eta == 0 ⇔ no change pending for that slot)
    // ---------------------------------------------------------------------------------------------------------

    address public pendingVerifier;
    uint256 public pendingVerifierEta;

    address public pendingOwnerReturn;
    uint256 public pendingOwnerReturnEta;

    address public pendingGovernance;
    uint256 public pendingGovernanceEta;

    // ---------------------------------------------------------------------------------------------------------
    // Events
    // ---------------------------------------------------------------------------------------------------------

    /// @notice Emitted on a successful peg-in. `trans` is the nullifier minted under (also the token's
    ///         `transactions` key); `stateRoot`/`height` are the proven Bismuth commitment.
    event PegInMinted(
        uint256 indexed lockId,
        address indexed ethRecipient,
        uint256 amount,
        bytes32 trans,
        bytes32 stateRoot,
        uint64 height
    );

    event Paused(address indexed by);
    event Unpaused(address indexed by);

    event VerifierChangeQueued(address indexed newVerifier, uint256 eta);
    event VerifierChangeExecuted(address indexed oldVerifier, address indexed newVerifier);
    event VerifierChangeCancelled(address indexed wouldBeVerifier);

    event OwnerReturnQueued(address indexed newOwner, uint256 eta);
    event OwnerReturnExecuted(address indexed newOwner);
    event OwnerReturnCancelled(address indexed wouldBeOwner);

    event GovernanceChangeQueued(address indexed newGovernance, uint256 eta);
    event GovernanceChangeExecuted(address indexed oldGovernance, address indexed newGovernance);
    event GovernanceChangeCancelled(address indexed wouldBeGovernance);

    // ---------------------------------------------------------------------------------------------------------
    // Constructor
    // ---------------------------------------------------------------------------------------------------------

    /// @param _wbis         The deployed wBIS token (this bridge becomes its owner via the deploy handoff).
    /// @param _verifier     The initial consensus verifier (e.g. `GuardianSetVerifier`).
    /// @param _vaultAddress The Bismuth vault address as its 56-byte ASCII string.
    /// @param _governance   The governance principal (multisig/timelock recommended).
    ///
    /// @dev DEPLOY HANDOFF: after deployment, the *current* wBIS owner calls
    ///      `wBIS.transferOwnership(address(thisBridge))` on the token (the live token's `Ownable` is the
    ///      single-step 0.4.x variant — no accept step). The bridge then holds mint authority and the peg is
    ///      live. To later migrate to a new bridge, governance uses the timelocked `returnWbisOwnership`.
    constructor(
        IWBIS _wbis,
        IBismuthConsensusVerifier _verifier,
        bytes memory _vaultAddress,
        address _governance
    ) {
        require(address(_wbis) != address(0), "wbis=0");
        require(address(_verifier) != address(0), "verifier=0");
        require(_vaultAddress.length == 56, "vault!=56");
        require(_governance != address(0), "gov=0");

        wbis = _wbis;
        verifier = _verifier;
        vaultAddress = _vaultAddress;
        governance = _governance;
    }

    // ---------------------------------------------------------------------------------------------------------
    // Modifiers
    // ---------------------------------------------------------------------------------------------------------

    modifier onlyGovernance() {
        require(msg.sender == governance, "not governance");
        _;
    }

    modifier whenNotPaused() {
        require(!paused, "paused");
        _;
    }

    // ---------------------------------------------------------------------------------------------------------
    // Peg-in (the value path)
    // ---------------------------------------------------------------------------------------------------------

    /// @notice Mint `amount` wBIS to `ethRecipient` against a native-BIS lock proven under a finalized Bismuth
    ///         state root. Permissionless: anyone can submit the proof (the recipient is bound INSIDE the
    ///         committed state, not derived from `msg.sender`), so a relayer can pay gas for a user.
    ///
    /// @dev    The leaf preimages are rebuilt here from the CLAIM (`amount`, `ethRecipient`, `lockId`), so a
    ///         set of passing Merkle proofs cryptographically binds exactly that amount + recipient to the
    ///         committed Bismuth state — a forged claim simply will not produce leaves that are in the tree.
    ///
    /// @param height          The Bismuth block height of `stateRoot` (passed through to `verifier`).
    /// @param stateRoot       The Bismuth consensus Merkle state root the lock is committed under.
    /// @param attestation     Opaque proof of finality consumed by `verifier` (guardian sigs today; zk later).
    /// @param lockId          The vault lock id `n >= 1` (slot 0 is the lock counter, never a lock).
    /// @param amount          Atomic units locked == wBIS base units to mint (`0 < amount <= MAX_LOCK`).
    /// @param ethRecipient    The 20-byte ETH recipient committed in the lock's recipient words.
    /// @param amountProof     Merkle inclusion proof for the amount leaf (slot `lockId*16`).
    /// @param recipientProofs The 10 Merkle inclusion proofs for the recipient chunks (slots `lockId*16+1+j`).
    function pegInMint(
        uint64 height,
        bytes32 stateRoot,
        bytes calldata attestation,
        uint256 lockId,
        uint256 amount,
        address ethRecipient,
        BismuthMerkle.Step[] calldata amountProof,
        BismuthMerkle.Step[][10] calldata recipientProofs
    ) external whenNotPaused nonReentrant {
        // --- Checks -------------------------------------------------------------------------------------
        require(lockId >= 1 && amount > 0 && amount <= MAX_LOCK, "bad lock/amount");

        // (2) Trust root: only proceed for a root the verifier attests Bismuth has FINALIZED.
        require(verifier.verifyStateRoot(stateRoot, height, attestation), "unverified root");

        // (3) Nullifier bound ONLY to (vault, lockId) — idempotent across roots/heights; one mint per lock.
        bytes32 trans = keccak256(abi.encodePacked("BIS-PEGIN:", vaultAddress, lockId));
        require(!consumed[trans], "already consumed");

        // --- Effects ------------------------------------------------------------------------------------
        consumed[trans] = true;

        // (4a) Amount leaf: slot = lockId*16, value = amount.
        {
            uint256 slot = lockId * 16;
            bytes memory preimage = abi.encodePacked(
                bytes1("S"),
                vaultAddress,
                bytes1(":"),
                bytes32(slot),
                bytes32(amount)
            );
            require(
                BismuthMerkle.verify(stateRoot, BismuthMerkle.leafHash(preimage), amountProof),
                "bad amount proof"
            );
        }

        // (4b) Recipient chunks j = 0..9: slot = lockId*16 + 1 + j, value = SENTINEL | ethRecipient[2j:2j+2].
        //      The 20-byte address is committed as ten 16-bit big-endian chunks, each stored by the vault with
        //      a sentinel bit 16 set (value in [0x10000, 0x1FFFF]). chunk j = bytes[2j:2j+2]: shift the address
        //      right by 8*(18 - 2*j) bits and truncate to uint16. j=0 → bytes[0:2] (high), j=9 → bytes[18:20]
        //      (low). Verified against the oracle (lock 1 chunk0 == 0x10000|0xf4f8).
        //
        //      WHY 16-bit chunks + sentinel (this FIXES the zero-stranding bug): a single VM storage slot holds
        //      at most 32 bits, and `vm_state.commit_storage` drops a 0 value as a DELETION (so a 0 slot has no
        //      Merkle leaf and is unprovable). A 32-bit recipient word can be 0 (e.g. a leading-zero / vanity
        //      address) → its lock would be permanently unredeemable. Splitting into <32-bit chunks leaves room
        //      for the always-set 0x10000 bit, so EVERY recorded chunk is non-zero, always provable; we require
        //      ALL ten (skipping one would let a forged claim redirect the lock). See bridge_vault.py.
        for (uint256 j = 0; j < 10; j++) {
            uint256 slot = lockId * 16 + 1 + j;
            uint256 chunk = uint256(uint16(uint160(ethRecipient) >> (8 * (18 - 2 * j))));
            bytes memory preimage = abi.encodePacked(
                bytes1("S"),
                vaultAddress,
                bytes1(":"),
                bytes32(slot),
                bytes32(uint256(0x10000) | chunk)
            );
            require(
                BismuthMerkle.verify(stateRoot, BismuthMerkle.leafHash(preimage), recipientProofs[j]),
                "bad recipient proof"
            );
        }

        // --- Interactions -------------------------------------------------------------------------------
        // (5) Mint via the token. The token ALSO enforces its own `transactions[trans]` replay guard, so the
        //     nullifier is double-checked across the bridge boundary.
        require(wbis.mint(ethRecipient, amount, trans), "mint failed");

        // (6) Observability for relayers / indexers.
        emit PegInMinted(lockId, ethRecipient, amount, trans, stateRoot, height);
    }

    // ---------------------------------------------------------------------------------------------------------
    // Governance — immediate safety switch
    // ---------------------------------------------------------------------------------------------------------

    /// @notice Halt peg-ins immediately (safety cannot wait for the timelock). Does not affect the trust knobs.
    function pause() external onlyGovernance {
        paused = true;
        emit Paused(msg.sender);
    }

    /// @notice Resume peg-ins.
    function unpause() external onlyGovernance {
        paused = false;
        emit Unpaused(msg.sender);
    }

    // ---------------------------------------------------------------------------------------------------------
    // Governance — immediate: forward the TOKEN-level pause brake
    // ---------------------------------------------------------------------------------------------------------
    // Once this bridge becomes the wBIS `owner`, the token's own `onlyOwner` `pause()`/`unpause()` are only
    // reachable through the owner — i.e. through this contract. Without these passthroughs the live token's
    // Pausable emergency brake (a pre-existing control on a token with real holders + an on-chain pool) would
    // be silently destroyed the moment ownership transfers. We forward it, immediate (a token-level incident
    // can't wait for the 2-day `returnWbisOwnership` timelock), gated to `governance`. NOTE: pausing the token
    // halts wBIS TRANSFERS but NOT minting; `pause()` above is the brake on this bridge's peg-in mint path.

    /// @notice Pause the wBIS TOKEN (its `Pausable`), forwarding through the bridge's owner authority.
    function pauseWbis() external onlyGovernance {
        wbis.pause();
    }

    /// @notice Unpause the wBIS token.
    function unpauseWbis() external onlyGovernance {
        wbis.unpause();
    }

    // ---------------------------------------------------------------------------------------------------------
    // Governance — timelocked: swap the trust root
    // ---------------------------------------------------------------------------------------------------------

    /// @notice Queue a verifier swap (e.g. guardian-set → zk). Executable after `CHANGE_DELAY`.
    function queueSetVerifier(address newVerifier) external onlyGovernance {
        require(newVerifier != address(0), "verifier=0");
        pendingVerifier = newVerifier;
        pendingVerifierEta = block.timestamp + CHANGE_DELAY;
        emit VerifierChangeQueued(newVerifier, pendingVerifierEta);
    }

    /// @notice Execute a previously-queued verifier swap once its eta has elapsed.
    function setVerifier() external onlyGovernance {
        uint256 eta = pendingVerifierEta;
        require(eta != 0, "none queued");
        require(block.timestamp >= eta, "timelock");

        address oldVerifier = address(verifier);
        address newVerifier = pendingVerifier;
        verifier = IBismuthConsensusVerifier(newVerifier);

        pendingVerifier = address(0);
        pendingVerifierEta = 0;
        emit VerifierChangeExecuted(oldVerifier, newVerifier);
    }

    /// @notice Cancel a queued verifier swap.
    function cancelSetVerifier() external onlyGovernance {
        require(pendingVerifierEta != 0, "none queued");
        address wouldBe = pendingVerifier;
        pendingVerifier = address(0);
        pendingVerifierEta = 0;
        emit VerifierChangeCancelled(wouldBe);
    }

    // ---------------------------------------------------------------------------------------------------------
    // Governance — timelocked: hand wBIS ownership to a new owner (e.g. a migrated bridge)
    // ---------------------------------------------------------------------------------------------------------

    /// @notice Queue returning wBIS mint authority to `newOwner` (e.g. a new bridge version). Executable after
    ///         `CHANGE_DELAY`. After execution this contract is no longer the wBIS owner and can no longer mint.
    function queueReturnWbisOwnership(address newOwner) external onlyGovernance {
        require(newOwner != address(0), "owner=0");
        pendingOwnerReturn = newOwner;
        pendingOwnerReturnEta = block.timestamp + CHANGE_DELAY;
        emit OwnerReturnQueued(newOwner, pendingOwnerReturnEta);
    }

    /// @notice Execute the queued wBIS-ownership handoff once its eta has elapsed.
    function returnWbisOwnership() external onlyGovernance {
        uint256 eta = pendingOwnerReturnEta;
        require(eta != 0, "none queued");
        require(block.timestamp >= eta, "timelock");

        address newOwner = pendingOwnerReturn;
        pendingOwnerReturn = address(0);
        pendingOwnerReturnEta = 0;

        wbis.transferOwnership(newOwner);
        emit OwnerReturnExecuted(newOwner);
    }

    /// @notice Cancel a queued wBIS-ownership handoff.
    function cancelReturnWbisOwnership() external onlyGovernance {
        require(pendingOwnerReturnEta != 0, "none queued");
        address wouldBe = pendingOwnerReturn;
        pendingOwnerReturn = address(0);
        pendingOwnerReturnEta = 0;
        emit OwnerReturnCancelled(wouldBe);
    }

    // ---------------------------------------------------------------------------------------------------------
    // Governance — timelocked: rotate the governance principal
    // ---------------------------------------------------------------------------------------------------------

    /// @notice Queue a governance handover. Executable after `CHANGE_DELAY`.
    function queueSetGovernance(address newGovernance) external onlyGovernance {
        require(newGovernance != address(0), "gov=0");
        pendingGovernance = newGovernance;
        pendingGovernanceEta = block.timestamp + CHANGE_DELAY;
        emit GovernanceChangeQueued(newGovernance, pendingGovernanceEta);
    }

    /// @notice Execute the queued governance handover once its eta has elapsed.
    function setGovernance() external onlyGovernance {
        uint256 eta = pendingGovernanceEta;
        require(eta != 0, "none queued");
        require(block.timestamp >= eta, "timelock");

        address oldGovernance = governance;
        address newGovernance = pendingGovernance;
        governance = newGovernance;

        pendingGovernance = address(0);
        pendingGovernanceEta = 0;
        emit GovernanceChangeExecuted(oldGovernance, newGovernance);
    }

    /// @notice Cancel a queued governance handover.
    function cancelSetGovernance() external onlyGovernance {
        require(pendingGovernanceEta != 0, "none queued");
        address wouldBe = pendingGovernance;
        pendingGovernance = address(0);
        pendingGovernanceEta = 0;
        emit GovernanceChangeCancelled(wouldBe);
    }
}
