// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title IWBIS — interface of the already-deployed wBIS token (doc/45, SPEC §1)
/// @notice This is the EXACT surface of the verified, live wBIS contracts that the bridge
///         integrates against — it is NOT redeployed. The original token is Solidity 0.4.23,
///         `decimals = 8`, Ownable (renounce blocked) + Pausable.
///         Live addresses (EIP-55-checksum before hard-coding anywhere — see `fixtures/`):
///           - ETH wBIS  0xf5cb350b40726b5bcf170d12e162b6193b291b41
///           - BSC wBIS  0x56672ecb506301b1e32ed28552797037c54d36a9
/// @dev Mint authority is the token `owner()`. The peg is made non-custodial by transferring
///      that ownership to `BismuthBridge`, which mints only against a Merkle-proven Bismuth lock.
interface IWBIS {
    /// @notice Emitted on a successful mint. `trans` is the per-mint nullifier.
    /// @param to     recipient credited with `amount` base units
    /// @param amount minted amount in wBIS base units (8 decimals)
    /// @param trans  the unique mint nullifier (non-zero; consumed in `transactions`)
    event Mint(address indexed to, uint256 amount, bytes32 trans);

    /// @notice Emitted when a holder burns wBIS to peg out to native BIS.
    /// @param burner the account whose balance was burned (msg.sender of `burn`)
    /// @param value  burned amount in wBIS base units
    /// @param addr   the destination Bismuth address bytes (length <= 112)
    event Burn(address indexed burner, uint256 value, bytes addr);

    /// @notice Mint `_amount` wBIS to `_to`, keyed by the unique nullifier `_trans`.
    /// @dev Callable only by the token owner (`hasMintPermission == onlyOwner`). The token's
    ///      internal `_mint` requires `_trans != 0 && !transactions[_trans]` and then sets
    ///      `transactions[_trans] = true`, so `_trans` is a built-in per-mint NULLIFIER that
    ///      prevents replay independently of the bridge. Emits `Mint(_to,_amount,_trans)` and
    ///      `Transfer(0,_to,_amount)`.
    /// @return success true on success.
    function mint(address _to, uint256 _amount, bytes32 _trans) external returns (bool success);

    /// @notice Permissionless relayed mint: anyone may submit an owner-authorized mint.
    /// @dev `approvalData` MUST be the owner's EIP-191 signature over
    ///      `keccak256(abi.encodePacked(_to, _amount, _trans))`. Subject to the same
    ///      `transactions[_trans]` replay protection as `mint`.
    /// @return success true on success.
    function relayMint(
        address _to,
        uint256 _amount,
        bytes32 _trans,
        bytes calldata approvalData
    ) external returns (bool success);

    /// @notice Burn `value` wBIS from `msg.sender` and commit a native-BIS destination address.
    /// @dev `addr.length` MUST be <= 112. Emits `Burn(msg.sender, value, addr)`. This is the
    ///      peg-OUT entry: the Bismuth-side release contract consumes a proof of this `Burn` log.
    function burn(uint256 value, bytes calldata addr) external;

    /// @notice The current token owner (the mint authority).
    function owner() external view returns (address);

    /// @notice Transfer token ownership (and thus mint authority). `onlyOwner`.
    function transferOwnership(address newOwner) external;

    /// @notice Pause token transfers/mints. `onlyOwner`.
    function pause() external;

    /// @notice Unpause the token. `onlyOwner`.
    function unpause() external;

    /// @notice Whether the token is currently paused.
    function paused() external view returns (bool);

    /// @notice wBIS base-unit balance of `account`.
    function balanceOf(address account) external view returns (uint256);

    /// @notice Standard ERC-20 transfer.
    function transfer(address to, uint256 amount) external returns (bool);

    /// @notice Token decimals — fixed at 8, matching native BIS atomic units (1e-8 BIS).
    function decimals() external view returns (uint8);
}
