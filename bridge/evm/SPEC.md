# wBIS Bridge — EVM side build spec (doc/45)

This is the authoritative spec for the Ethereum/BNB-side contracts that **link native ("physical") BIS to
the already-deployed wBIS token**. Every contract below must match these signatures, constants and encodings
exactly; the Python oracle (`oracle/gen_vectors.py` → `fixtures/vectors.json`) is the cross-chain source of
truth and the Hardhat tests assert against it.

## 0. The link, in one sentence
The deployed wBIS token already has a bridge-shaped mint/burn API whose mint authority is its `owner`. We make
the peg **non-custodial** by transferring that ownership to **`BismuthBridge`**, which mints wBIS *only* when it
verifies, on-chain, a Bismuth lock committed under Bismuth's consensus state root. No operator key on the value
path; the only pluggable trust is the consensus attestation (interim guardian set today → zk-proof endgame).

## 1. The already-deployed wBIS token (DO NOT redeploy — integrate against it)
Verified source (Etherscan/Blockscout). Solidity 0.4.23. `decimals = 8`. Ownable (renounce blocked) + Pausable.

```
function mint(address _to, uint256 _amount, bytes32 _trans) public hasMintPermission returns (bool);
   // hasMintPermission == onlyOwner. _mint requires _trans != 0 && !transactions[_trans]; sets transactions[_trans]=true.
   // => _trans is a built-in per-mint NULLIFIER. emits Mint(to,amount,trans) + Transfer(0,to,amount).
function relayMint(address _to, uint256 _amount, bytes32 _trans, bytes approvalData) public returns (bool);
   // permissionless submit; approvalData must be owner's EIP-191 sig over keccak256(abi.encodePacked(_to,_amount,_trans)).
function burn(uint256 value, bytes addr) public;   // burns msg.sender; _addr.length<=112; emits Burn(burner,value,addr).
function owner() view returns (address);
function transferOwnership(address newOwner) public; // onlyOwner
function pause()/unpause()/paused();
event Mint(address indexed to, uint256 amount, bytes32 trans);
event Burn(address indexed burner, uint256 value, bytes addr);
```
Live addresses (EIP-55-checksum before hard-coding — see `fixtures/`):
- ETH wBIS `0xf5cb350b40726b5bcf170d12e162b6193b291b41`; ETH/wBIS Uniswap pool `0xf4f82f8d84c529987201609cecee8ab136a50c8c`
- BSC wBIS `0x56672ecb506301b1e32ed28552797037c54d36a9`; BNB/wBIS pool `0x731b8244f818fd488d9dc516edd976a96459ae59`

## 2. Native↔wBIS units
Native BIS atomic unit = 1e-8 BIS (`amounts.SATOSHIS_PER_BIS = 1e8`). wBIS `decimals = 8`. **1:1 at atomic
units** — a lock of `A` atomic units mints exactly `A` wBIS base units. No scaling.

## 3. The Bismuth lock being proven (peg-in)
`contracts/bridge_vault.py` (`FN_LOCK`) records each lock in VM storage. For lock id `n` (n≥1), at the vault's
56-hex contract address `V` (stride 16):
```
slot[n*16 + 0]      = amount                       (uint, 0 < amount <= 0xFFFFFFFF — VM callvalue is 32-bit)
slot[n*16 + 1 + j]  = 0x10000 | uint16_be(eth_recipient[2j : 2j+2]),  j = 0..9   (the 20-byte ETH recipient)
```
(Slot 0 holds the lock counter — never a lock; that's why n≥1.) The recipient is committed as ten 16-bit
big-endian chunks, each with a **sentinel bit `0x10000` set** so every stored value is in `[0x10000, 0x1FFFF]`
— never `0`. This is required because `vm_state.commit_storage` drops a `0` value as a deletion (an all-zero
recipient chunk, e.g. a leading-zero / vanity address, would otherwise be unprovable → the locked BIS would be
stranded). 16-bit chunks (not 32-bit words) leave room for the sentinel inside a 32-bit slot. The verifier
strips it: `chunk = value & 0xFFFF`, with `value >> 16 == 1`.

### 3.1 Canonical state-leaf preimage (from `vm_state._state_entries`; `_KEY = 32`)
A storage slot's Merkle **leaf preimage** is:
```
preimage = "S"  ||  V_ascii(56 bytes)  ||  ":"  ||  be32(slot)  ||  be32(value)
```
where `V_ascii` is the vault address STRING as ASCII bytes (56 bytes), `be32(x)=bytes32(uint256(x))`.
So each leaf preimage is `1 + 56 + 1 + 32 + 32 = 122` bytes. (Solidity: `abi.encodePacked(bytes1("S"),
vaultAddr, bytes1(":"), bytes32(slot), bytes32(value))`, `vaultAddr` stored as the 56-byte ASCII string.)

## 4. The Merkle commitment (`vm_merkle.py` — MUST match byte-for-byte)
- Hash = **BLAKE2b-256** (`hashlib.blake2b(digest_size=32)`, unkeyed).
- `leafHash  = blake2b(0x00 || preimage)`           (LEAF_TAG = 0x00)
- `nodeHash  = blake2b(0x01 || left || right)`       (NODE_TAG = 0x01)
- A **lone node at a level is PROMOTED unchanged** (carried up), never duplicated (CVE-2012-2459 safe).
- `EMPTY_ROOT = blake2b("bismuth-vm-merkle/empty-state")` (see `fixtures/vectors.json.emptyRoot`).
- Proof = one element per level from leaf to root: `(sibling, siblingIsLeft)`, or **promoted** (no sibling) at
  a level where the node had no pair. `verify`: `h=leafHash; for each step: if promoted continue;
  h = siblingIsLeft ? node(sibling,h) : node(h,sibling); return h==root`. **Needs no tree size.**

## 5. BLAKE2b in Solidity via the EIP-152 precompile (address `0x09`) — the correctness linchpin
Precompile `0x09` computes ONE BLAKE2b F compression. Input is EXACTLY 213 bytes, output 64 bytes:
```
input  = be32(rounds=12) || h[64 bytes] || m[128 bytes] || t[16 bytes] || f[1 byte]
         where h = 8×uint64 LITTLE-endian (state), m = 16×uint64 LITTLE-endian (msg block),
               t = t0,t1 each uint64 LITTLE-endian (byte counter), f = 0x01 on the final block else 0x00.
output = new h = 8×uint64 LITTLE-endian.
```
Full unkeyed BLAKE2b-256 of arbitrary `input`:
1. `h = IV` (the 8 standard BLAKE2b IV constants), then `h[0] ^= 0x01010020` (param block: digest_len=0x20,
   key_len=0, fanout=1, depth=1 → low word `0x...01010020`).
2. Process message in 128-byte blocks. Maintain total byte counter. For every block but the last: `t += 128`,
   `f = 0`, call `0x09`. Last block: `t = total_len`, `f = 1`, zero-pad the block to 128 bytes, call `0x09`.
   Empty input = a single final block of 128 zero bytes with `t = 0`, `f = 1`.
3. Digest = first 32 bytes of `h` serialized LITTLE-endian (i.e. byte-swap h[0..3]).
**Endianness is THE trap:** precompile h/m/t are little-endian uint64; `bytes32`/`encodePacked` are big-endian —
byte-swap on the way in and out. `Blake2b.sol` MUST pass every vector in `fixtures/vectors.json.blake2bKats`.

## 6. Files to produce (all under `contracts/`, Solidity ^0.8.24, MIT)
| File | Responsibility |
|---|---|
| `interfaces/IWBIS.sol` | exact interface of §1 (mint/relayMint/burn/owner/transferOwnership/pause/paused/balanceOf/transfer/decimals + Mint/Burn events). |
| `interfaces/IBismuthConsensusVerifier.sol` | `function verifyStateRoot(bytes32 stateRoot, uint64 height, bytes calldata attestation) external view returns (bool);` — the single pluggable trust root. MUST only return true for a root Bismuth has **finalized** (the impl's responsibility). |
| `lib/Blake2b.sol` | `function hash256(bytes memory input) internal view returns (bytes32)` — §5; matches `hashlib.blake2b(digest_size=32)`. |
| `lib/BismuthMerkle.sol` | constants `LEAF_TAG=0x00`,`NODE_TAG=0x01`,`EMPTY_ROOT`; `struct Step{bytes32 sibling; bool isLeft; bool present;}`; `leafHash(bytes)`, `nodeHash(bytes32,bytes32)`, `verify(bytes32 root, bytes32 leafHash, Step[] memory proof) returns(bool)` — §4. `present==false` ⇔ vm_merkle promoted level. |
| `BismuthBridge.sol` | the core (see §7). |
| `verifiers/GuardianSetVerifier.sol` | interim M-of-N guardian verifier (see §8). |
| `verifiers/ZkBismuthVerifier.sol` + `interfaces/IGroth16Verifier.sol` | endgame zk adapter (see §8). |
| `mocks/MockWBIS.sol` | faithful 0.8 re-impl of §1 for tests (Ownable+Pausable, mint onlyOwner with `transactions[trans]` replay map, relayMint, burn(value,bytes), decimals=8, Mint/Burn events). |

## 7. `BismuthBridge.sol`
State: `IWBIS public wbis; IBismuthConsensusVerifier public verifier; bytes public vaultAddress (56 ASCII bytes);
mapping(bytes32=>bool) public consumed; address public governance; uint256 public constant MAX_LOCK = type(uint32).max;`
plus a minimal timelock for verifier/ownership changes and an immediate `paused` flag.

Peg-in entry:
```
struct Step { bytes32 sibling; bool isLeft; bool present; }   // re-exported from BismuthMerkle
function pegInMint(
    uint64 height, bytes32 stateRoot, bytes calldata attestation,
    uint256 lockId, uint256 amount, address ethRecipient,
    Step[] calldata amountProof, Step[][10] calldata recipientProofs
) external whenNotPaused nonReentrant;
```
Logic (checks-effects-interactions):
1. `require(lockId >= 1 && amount > 0 && amount <= MAX_LOCK)`.
2. `require(verifier.verifyStateRoot(stateRoot, height, attestation))` — trust root (finality is its job).
3. `bytes32 trans = keccak256(abi.encodePacked("BIS-PEGIN:", vaultAddress, lockId))` — nullifier, bound ONLY to
   (vault, lockId) so it's idempotent across roots/heights. `require(!consumed[trans]); consumed[trans]=true;`
4. Verify the 11 leaves against `stateRoot`, each preimage rebuilt from the **claim** (so a passing proof binds
   amount+recipient to committed state):
   - amount: `slot=lockId*16`, `value=amount`.
   - recipient chunk j (0..9): `slot=lockId*16+1+j`, `value=0x10000 | uint16(ethRecipient >> (8*(18-2*j)))`
     (chunk j = `ethRecipient[2j:2j+2]` big-endian, with the sentinel bit; verify your shift against the oracle).
   - build preimage per §3.1, `leafHash=BismuthMerkle.leafHash(preimage)`, `require(BismuthMerkle.verify(stateRoot, leafHash, proof))`.
5. `require(wbis.mint(ethRecipient, amount, trans))`. (Token also enforces its own `transactions[trans]` — double safety.)
6. `emit PegInMinted(lockId, ethRecipient, amount, trans, stateRoot, height);`

Governance (a multisig/timelock as `governance`): `setVerifier(newVerifier)` and `returnWbisOwnership(addr)` are
**timelocked** (e.g. `CHANGE_DELAY = 2 days`: queue→eta→execute) so a compromised key cannot instantly swap the
trust root and mint infinitely. `pause()/unpause()` are immediate (safety can't wait). `acceptWbisOwnership()`
helper (calls `wbis.transferOwnership` is on the token; the bridge becomes owner via the token's
`transferOwnership(bridge)` then this contract holds it). Document the deploy handoff in §9.

Peg-OUT is NOT on the bridge: a holder calls `wBIS.burn(amount, bismuthAddrBytes)` directly on the token; the
Bismuth-side release contract consumes a proof of that `Burn` log. Document, don't implement here.

## 8. Verifiers (the only trust knob)
- `GuardianSetVerifier` (interim, deployable now — a federation; label it honestly): guardian set + `threshold`.
  `attestation = abi.encode(bytes[] sigs)`; each sig is an EIP-191 (`toEthSignedMessageHash`) signature over
  `keccak256(abi.encode(block.chainid, address(this), stateRoot, height))`. Count **distinct** guardians whose
  recovered signer ∈ set; `return count >= threshold`. Guardians sign only finalized roots (their duty). Set +
  threshold are governance-timelocked. This is honest-majority trust — the documented interim, NOT the endgame.
- `ZkBismuthVerifier` (endgame): `constructor(IGroth16Verifier groth)`; `verifyStateRoot` decodes
  `attestation=(uint[2] a,uint[2][2] b,uint[2] c)` and calls `groth.verifyProof(a,b,c, [uint(stateRoot), height])`.
  `IGroth16Verifier` is the snarkjs/gnark-generated verifier from the Stage-3 circuit (proves Bismuth PoW header
  chain + cumulative work + this state root). The circuit/verifying-key is NOT in-repo (frontier, doc/45 §3
  Stage 3) — this adapter is the drop-in seam, marked clearly.

## 9. Trust framing (state it exactly; do not overclaim)
Trustless & final on-chain: the mint/burn wiring, the `_trans` nullifier (bridge + token), the BLAKE2b/Merkle
inclusion check, units, pause, timelock. The ONE pluggable trust assumption is `IBismuthConsensusVerifier`:
ships as `GuardianSetVerifier` (honest M-of-N — go live today) and upgrades, via timelocked `setVerifier`, to
`ZkBismuthVerifier` (only-the-two-chains'-consensus, doc/45's goal) with NO wBIS-ownership migration. Per-lock
amount ≤ `0xFFFFFFFF` atomic units (~42.94 BIS) — a documented VM-callvalue limitation (`vm_engine.py:203`);
larger transfers use multiple locks until the VM widens callvalue.

## 10. Tests (Hardhat, assert against `fixtures/vectors.json`)
1. `Blake2b`: every `blake2bKats[i].blake2b256` reproduced by `hash256(input)`.
2. `BismuthMerkle`: `EMPTY_ROOT` matches; each lock slot's `leafHash` reproduced; `verify(stateRoot, leafHash, proof)`
   true; a tampered sibling/flipped isLeft/missing-promotion ⇒ false.
3. `BismuthBridge` (with `MockWBIS` + a stub verifier returning true): full `pegInMint` mints `amount`→`ethRecipient`;
   replay reverts (bridge nullifier AND token `transactions`); wrong amount/recipient (bad proof) reverts;
   `lockId=0` reverts; `amount>MAX_LOCK` reverts; paused reverts; verifier=false reverts.
4. `GuardianSetVerifier`: 2-of-3 passes, 1 sig fails, duplicate-signer doesn't double-count, wrong-root fails.
5. Governance: `setVerifier`/`returnWbisOwnership` enforce the timelock; `pause` immediate.

The JS test converts an oracle proof element `{sibling, siblingIsLeft}` → `Step{ sibling: sibling||ZERO,
isLeft: siblingIsLeft, present: sibling!=null }`. recipient chunk j = `0x10000 | uint16(addr >> 8*(18-2j))`:
verify against the vector values (incl. lock 3, a leading-zero recipient whose chunks 0–1 are exactly 0x10000).
