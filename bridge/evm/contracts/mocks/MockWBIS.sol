// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title MockWBIS
/// @notice Faithful Solidity 0.8 re-implementation of the already-deployed wBIS token
///         (SPEC.md §1; original is Solidity 0.4.23, Ownable + Pausable, decimals = 8).
///         FOR TESTS ONLY — the real token is integrated against, never redeployed.
///
/// Externally-observable behaviour mirrored here:
///   - ERC-20: name "Wrapped BIS", symbol "wBIS", decimals 8; balanceOf/transfer/transferFrom/
///     approve/allowance/totalSupply with Transfer/Approval events.
///   - Ownable: `owner`, `transferOwnership(newOwner)` (onlyOwner); renounce is BLOCKED (reverts).
///   - Pausable: `pause()`/`unpause()`/`paused()` (onlyOwner); transfers blocked while paused.
///   - Bridge mint API: `mint(_to,_amount,_trans)` (onlyOwner) with a built-in per-mint NULLIFIER
///     `transactions[_trans]`; `relayMint(...)` (permissionless, owner EIP-191 signature); both route
///     through the same `_mint`. `burn(value, addr)` burns msg.sender; `addr.length <= 112`.
contract MockWBIS {
    // ----------------------------------------------------------------- ERC-20 metadata
    string public constant name = "Wrapped BIS";
    string public constant symbol = "wBIS";
    uint8 public constant decimals = 8;

    // ----------------------------------------------------------------- ERC-20 ledger
    uint256 internal totalSupply_;
    mapping(address => uint256) internal balances;
    mapping(address => mapping(address => uint256)) internal allowed;

    // ----------------------------------------------------------------- bridge nullifier
    /// @notice per-mint replay guard: a `_trans` may be minted at most once (built into `_mint`).
    mapping(bytes32 => bool) public transactions;

    // ----------------------------------------------------------------- Ownable
    address public owner;

    // ----------------------------------------------------------------- Pausable
    bool public paused;

    // ----------------------------------------------------------------- events
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event Mint(address indexed to, uint256 amount, bytes32 trans);
    event Burn(address indexed burner, uint256 value, bytes addr);
    event Pause();
    event Unpause();
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    // ----------------------------------------------------------------- modifiers
    modifier onlyOwner() {
        require(msg.sender == owner, "Ownable: caller is not the owner");
        _;
    }

    modifier whenNotPaused() {
        require(!paused, "Pausable: paused");
        _;
    }

    modifier whenPaused() {
        require(paused, "Pausable: not paused");
        _;
    }

    constructor() {
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    // ================================================================= ERC-20 views
    function totalSupply() public view returns (uint256) {
        return totalSupply_;
    }

    function balanceOf(address _owner) public view returns (uint256) {
        return balances[_owner];
    }

    function allowance(address _owner, address _spender) public view returns (uint256) {
        return allowed[_owner][_spender];
    }

    // ================================================================= ERC-20 transfers
    // Transfers are blocked while paused (PausableToken in the original).
    function transfer(address _to, uint256 _value) public whenNotPaused returns (bool) {
        require(_to != address(0), "ERC20: transfer to zero");
        require(_value <= balances[msg.sender], "ERC20: balance exceeded");
        balances[msg.sender] -= _value;
        balances[_to] += _value;
        emit Transfer(msg.sender, _to, _value);
        return true;
    }

    function transferFrom(address _from, address _to, uint256 _value) public whenNotPaused returns (bool) {
        require(_to != address(0), "ERC20: transfer to zero");
        require(_value <= balances[_from], "ERC20: balance exceeded");
        require(_value <= allowed[_from][msg.sender], "ERC20: allowance exceeded");
        balances[_from] -= _value;
        balances[_to] += _value;
        allowed[_from][msg.sender] -= _value;
        emit Transfer(_from, _to, _value);
        return true;
    }

    function approve(address _spender, uint256 _value) public whenNotPaused returns (bool) {
        allowed[msg.sender][_spender] = _value;
        emit Approval(msg.sender, _spender, _value);
        return true;
    }

    // ================================================================= bridge mint API
    /// @notice onlyOwner mint with built-in nullifier. The bridge becomes `owner` and calls this.
    function mint(address _to, uint256 _amount, bytes32 _trans) public onlyOwner returns (bool) {
        return _mint(_to, _amount, _trans);
    }

    /// @notice Permissionless submit; `approvalData` must be the owner's EIP-191 signature over
    ///         keccak256(abi.encodePacked(_to, _amount, _trans)) (toEthSignedMessageHash convention).
    function relayMint(
        address _to,
        uint256 _amount,
        bytes32 _trans,
        bytes calldata approvalData
    ) public returns (bool) {
        bytes32 message = keccak256(abi.encodePacked(_to, _amount, _trans));
        bytes32 ethSigned = keccak256(abi.encodePacked("\x19Ethereum Signed Message:\n32", message));
        address signer = _recover(ethSigned, approvalData);
        require(signer != address(0) && signer == owner, "relayMint: bad signature");
        return _mint(_to, _amount, _trans);
    }

    /// @dev The single mint path. Enforces the `_trans` nullifier exactly like the deployed token's
    ///      `_mint`: requires `_trans != 0 && !transactions[_trans]`, then sets it. Emits Mint + Transfer.
    function _mint(address _to, uint256 _amount, bytes32 _trans) internal returns (bool) {
        require(_trans != bytes32(0), "mint: trans is zero");
        require(!transactions[_trans], "mint: trans already used");
        transactions[_trans] = true;
        totalSupply_ += _amount;
        balances[_to] += _amount;
        emit Mint(_to, _amount, _trans);
        emit Transfer(address(0), _to, _amount);
        return true;
    }

    // ================================================================= burn (peg-out)
    /// @notice Burns the caller's tokens, recording the destination Bismuth address bytes (<=112).
    function burn(uint256 value, bytes calldata addr) public {
        require(addr.length <= 112, "burn: addr too long");
        require(value <= balances[msg.sender], "burn: balance exceeded");
        balances[msg.sender] -= value;
        totalSupply_ -= value;
        emit Burn(msg.sender, value, addr);
        emit Transfer(msg.sender, address(0), value);
    }

    // ================================================================= Ownable
    function transferOwnership(address newOwner) public onlyOwner {
        require(newOwner != address(0), "Ownable: new owner is zero");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    /// @notice Renouncing ownership is BLOCKED on the real token; mirror that by reverting.
    function renounceOwnership() public view onlyOwner {
        revert("Ownable: renounce is blocked");
    }

    // ================================================================= Pausable
    function pause() public onlyOwner whenNotPaused {
        paused = true;
        emit Pause();
    }

    function unpause() public onlyOwner whenPaused {
        paused = false;
        emit Unpause();
    }

    // ================================================================= ECDSA recover (EIP-191)
    /// @dev Recovers the signer of `hash` from a 65-byte {r,s,v} signature. Returns address(0) on
    ///      malformed input or non-canonical (high-s) signatures.
    function _recover(bytes32 hash, bytes calldata signature) internal pure returns (address) {
        if (signature.length != 65) {
            return address(0);
        }
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := calldataload(signature.offset)
            s := calldataload(add(signature.offset, 32))
            v := byte(0, calldataload(add(signature.offset, 64)))
        }
        if (v < 27) {
            v += 27;
        }
        if (v != 27 && v != 28) {
            return address(0);
        }
        // reject the upper-half (malleable) s per EIP-2
        if (uint256(s) > 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0) {
            return address(0);
        }
        return ecrecover(hash, v, r, s);
    }
}
