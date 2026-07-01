# doc/46 — wBIS EVM bridge: linking native BIS to the live wBIS token

**Status:** BUILT + tested (`bridge/evm/`, Hardhat, solc 0.8.24 — 21 EVM tests + 3 Bismuth-side vault tests
green). All consensus-touching pieces are hf2-gated. This is the **EVM half** of the non-custodial bridge
designed in [doc/45](45-bridge.md); doc/45 is the trust-model/overview, this doc is the implementation
reference for the Ethereum/BNB side and the Bismuth lock layout it consumes.

> **One sentence.** The live wBIS token already exposes a bridge-shaped, owner-gated `mint` and a `burn` that
> names a Bismuth address; we make the peg **non-custodial** by transferring the token's `owner` (mint
> authority) to **`BismuthBridge.sol`**, which mints wBIS *only* after re-verifying, fully on-chain, that an
> equal amount of native ("physical") BIS was locked in the Bismuth vault and committed under Bismuth's
> consensus state root. There is **no operator key and no stored secret on the value path** — authority is
> "the caller is the token owner" (EVM-enforced) plus signature/proof verification against *public* keys/roots.

---

## 1. The already-deployed wBIS token (integrate against — do NOT redeploy)

Verified source (Etherscan / Blockscout), Solidity **0.4.23**, `name "Wrapped BIS"`, `symbol "wBIS"`,
**`decimals = 8`**, `Ownable` (single-step; `renounceOwnership` overridden to revert) + `Pausable`.

```solidity
// mint authority is the OWNER. hasMintPermission == require(msg.sender == owner).
function mint(address _to, uint256 _amount, bytes32 _trans) public hasMintPermission returns (bool);

// permissionless SUBMIT of an owner-signed approval (EIP-191 over keccak256(abi.encodePacked(_to,_amount,_trans))).
function relayMint(address _to, uint256 _amount, bytes32 _trans, bytes approvalData) public returns (bool);
function whoMint(...) pure returns (address);   // helper: recovered signer
function msgMint(...) pure returns (bytes32);    // helper: the eth-signed digest

// the shared internal mint — note the BUILT-IN NULLIFIER:
function _mint(address _to, uint256 _amount, bytes32 _trans) internal returns (bool) {
    require(_trans != bytes32(0), "Empty tx");
    require(!transactions[_trans], "Existing tx");   // <-- per-mint replay guard
    transactions[_trans] = true;
    ... totalSupply += _amount; balances[_to] += _amount;
    emit Mint(_to, _amount, _trans); emit Transfer(address(0), _to, _amount);
}

// peg-OUT: burn naming a Bismuth recipient (bytes, <= 112 long).
function burn(uint256 value, bytes addr) public;   // emits Burn(msg.sender, value, addr) + Transfer(.,0,.)

function owner() view returns (address);
function transferOwnership(address newOwner) public onlyOwner;   // sets owner DIRECTLY (no pendingOwner/claim)
function pause()/unpause()/paused();   // onlyOwner; pause halts TRANSFERS, not minting
event Mint(address indexed to, uint256 amount, bytes32 trans);
event Burn(address indexed burner, uint256 value, bytes addr);
```

Three properties make this token bridge-ready out of the box:
1. **Owner-gated mint** — set `owner = BismuthBridge` and only the bridge can authorize a mint.
2. **`_trans` is a nullifier** — the token itself refuses to mint the same `_trans` twice. The bridge sets
   `_trans` to a per-lock id, giving **defense in depth** (bridge nullifier *and* token nullifier).
3. **`burn(value, bismuthAddr)`** — the peg-out leg is already emitted as a log a Bismuth release contract can
   prove against.

**Live addresses (EIP-55-checksummed; verified via ethers):**

| Chain | wBIS token | DEX pool |
|---|---|---|
| Ethereum | `0xf5cB350b40726B5BcF170d12e162B6193b291B41` | `0xF4F82f8d84C529987201609cecee8ab136A50c8c` (Uniswap v3) |
| BNB Chain | `0x56672ecb506301b1E32ED28552797037c54D36A9` | `0x731B8244F818FD488d9DC516Edd976A96459Ae59` |

> The bridge is chain-agnostic — the same `BismuthBridge` deploys on both, with the per-chain wBIS address as a
> constructor arg. The BNB wBIS is assumed to share this interface (it is the same wBIS); confirm its verified
> source before the handoff, or deploy a per-chain `IWBIS` shim if it differs.

---

## 2. Units — 1:1 at atomic units

Native BIS atomic unit = `1e-8 BIS` (`amounts.SATOSHIS_PER_BIS = 100_000_000`, 8 decimals). wBIS `decimals = 8`.
A lock of `A` atomic units mints exactly `A` wBIS base units. **No scaling anywhere.**

---

## 3. End-to-end flows

```
PEG-IN  (native BIS  ->  wBIS)
  user --lock A BIS, name eth_recipient--> bridge_vault (Bismuth VM)         [BIS held in vault custody]
       the lock (amount + recipient) is written to VM storage
       -> committed under the block's vm_state Merkle root (doc/45 Stage 2b)
  relayer (permissionless) reads the inclusion proofs + a finality attestation
       --pegInMint(root, height, attestation, lockId, amount, recipient, proofs)--> BismuthBridge (EVM)
  BismuthBridge: verify finality -> nullifier -> re-verify the 11 lock leaves vs root -> wBIS.mint(recipient, A, trans)

PEG-OUT (wBIS  ->  native BIS)
  holder --wBIS.burn(A, bismuthAddrBytes)--> wBIS token (EVM)   [emits Burn(holder, A, bismuthAddr)]
  relayer carries the burn log + an Ethereum finality/MPT proof --> Bismuth release contract
  release contract: verify the burn is in a finalized ETH block -> release A BIS to bismuthAddr
       (the eth_verify.py signer/commitment core is built; the MPT walk + BLS finality = doc/45 Stage 1b, pending)
```

Neither direction has a custodian or a signer-set on the value path: each chain re-verifies the *other chain's
consensus* in-contract. A relayer only carries data the destination independently re-checks — it cannot forge
or steal; a missing relayer only delays (liveness, not trust — doc/45 §1).

---

## 4. The Bismuth lock record (`contracts/bridge_vault.py`)

`FN_LOCK(eth_recipient)` (attach the BIS as call value) records lock id `n` (n ≥ 1; slot 0 is the counter) at
**stride 16**:

```
slot[n*16 + 0]      = amount                                   (uint, 0 < amount <= 0xFFFFFFFF)
slot[n*16 + 1 + j]  = 0x10000 | uint16_be(eth_recipient[2j : 2j+2]),   j = 0 .. 9
```

The 20-byte recipient is committed as **ten 16-bit big-endian chunks, each OR'd with a `0x10000` sentinel
bit** (so every stored value is in `[0x10000, 0x1FFFF]`).

### 4.1 Why the sentinel (a real fund-loss bug, found in review and fixed)
`vm_state.commit_storage` stores a slot value of `0` as a **deletion** (`0 == unset`, kept compact). If the
recipient were stored as five raw 32-bit words, a word of `0x00000000` — e.g. a leading-zero / "vanity" ETH
address, or any address with a 4-byte-aligned zero run — would be **absent from state and unprovable**. The
bridge requires a valid inclusion proof for *every* recipient slot (skipping one would let a forged claim
redirect the lock to a different address), so an all-zero word would make the lock **permanently unredeemable
— the locked BIS stranded forever.** A single VM storage value is at most 32 bits, and *any* 32-bit value can
be 0; splitting into **sub-32-bit chunks leaves room for an always-set sentinel bit**, so no chunk is ever 0.
The verifier strips it: `chunk = value & 0xFFFF` (and `value >> 16 == 1`). Regression-tested on both sides
(`tests/test_bridge_vault.py::test_zero_chunk_recipient_is_still_provable` + an EVM mint to
`0x00000000aabbccddeeff…`).

**Per-lock cap.** `amount <= 0xFFFFFFFF` atomic units (~42.94 BIS) — the VM exposes callvalue as a 32-bit word
and `vm_engine` refunds a deposit `> 0xFFFFFFFF` (`vm_engine.py`). Larger transfers use multiple locks; the cap
is a Bismuth-VM property, not an EVM-side one, and lifts when the VM widens callvalue.

### 4.2 Canonical state-leaf preimage (`vm_state._state_entries`, `_KEY = 32`)
The Merkle leaf **preimage** of a storage slot is:

```
preimage = "S" || V_ascii(56 bytes) || ":" || be32(slot) || be32(value)
```

where `V_ascii` is the vault's 56-hex contract address as **ASCII bytes** (56 bytes, *not* the decoded 28),
`be32(x) = bytes32(uint256(x))`. (Code/balance leaves use `"C"||addr||len4||code` and `"B"||addr||be32(bal)`;
`_state_entries` orders code, then storage, then balances, each in LMDB key order.) The Solidity side rebuilds
this with `abi.encodePacked(bytes1("S"), vaultAddress, bytes1(":"), bytes32(slot), bytes32(value))`.

---

## 5. The Merkle commitment and its Solidity port

### 5.1 `vm_merkle.py` (the consensus implementation — the spec)
Binary Merkle tree over `_state_entries`, BLAKE2b-256 (`hashlib.blake2b(digest_size=32)`):
- leaf = `H(0x00 || preimage)`, node = `H(0x01 || left || right)` (RFC-6962-style domain tags ⇒ second-preimage
  resistance);
- a **lone node at a level is PROMOTED** unchanged (never duplicated — CVE-2012-2459 safe);
- `EMPTY_ROOT = blake2b("bismuth-vm-merkle/empty-state")`;
- a proof carries one element per level: `(sibling, sibling_is_left)`, or **promoted** (no sibling). `verify`
  needs only `(root, leaf, proof)` — no tree size.

### 5.2 `lib/Blake2b.sol` — BLAKE2b-256 via the EIP-152 precompile (`0x09`)
The correctness linchpin. Precompile `0x09` computes one BLAKE2b **F** compression; input is exactly 213 bytes
(`be32(rounds=12) || h[64] || m[128] || t[16] || f[1]`, with `h`/`m`/`t` as **little-endian** uint64 words),
output 64 bytes. `hash256(bytes)` wraps it into a full unkeyed digest:
1. `h = IV`, then `h[0] ^= 0x01010020` (param block: digest-len 0x20, key 0, fanout 1, depth 1);
2. process 128-byte blocks updating the byte counter `t`; final block sets `f = 1`, zero-padded; empty input =
   one all-zero final block with `t = 0, f = 1`;
3. digest = first 32 bytes of `h` serialized **little-endian** (byte-swap on the way in and out — the endianness
   is the trap).

Validated against 7 `hashlib.blake2b(digest_size=32)` KATs incl. block boundaries (128/129/256 B). (Hardhat's
EVM provides `0x09` since Istanbul; we target `cancun`.)

### 5.3 `lib/BismuthMerkle.sol`
A 1:1 port of §5.1: `LEAF_TAG=0x00`, `NODE_TAG=0x01`, the `EMPTY_ROOT` constant (= the oracle value),
`struct Step { bytes32 sibling; bool isLeft; bool present; }` (`present == false` ⇔ vm_merkle's promoted
level), and `verify(root, leaf, Step[])` walking exactly like `vm_merkle.verify_proof`.

---

## 6. `BismuthBridge.sol` — the peg-in mint authority

```solidity
function pegInMint(
    uint64 height, bytes32 stateRoot, bytes calldata attestation,
    uint256 lockId, uint256 amount, address ethRecipient,
    BismuthMerkle.Step[] calldata amountProof,
    BismuthMerkle.Step[][10] calldata recipientProofs
) external whenNotPaused nonReentrant;
```

Checks → effects → interactions:
1. `require(lockId >= 1 && amount > 0 && amount <= MAX_LOCK)` (`MAX_LOCK == type(uint32).max`).
2. `require(verifier.verifyStateRoot(stateRoot, height, attestation))` — the single pluggable trust root; it
   alone decides whether `(stateRoot, height)` is a **finalized** Bismuth commitment (§7).
3. `bytes32 trans = keccak256(abi.encodePacked("BIS-PEGIN:", vaultAddress, lockId))` — the nullifier, bound
   **only** to `(vault, lockId)` so it is idempotent across every root/height that contains the lock;
   `require(!consumed[trans]); consumed[trans] = true;`.
4. Re-verify the **11 lock leaves** against `stateRoot`, each preimage **rebuilt from the call's claim**
   (amount @ `lockId*16`; chunk j @ `lockId*16+1+j`, value `0x10000 | uint16(ethRecipient >> 8*(18-2j))`). A
   passing proof set cryptographically binds *exactly that amount + recipient* to committed Bismuth state — a
   forged claim simply produces leaves that are not in the tree.
5. `require(wbis.mint(ethRecipient, amount, trans))` (the token's `transactions[trans]` double-guards).
6. `emit PegInMinted(lockId, ethRecipient, amount, trans, stateRoot, height)`.

**Governance** (`governance`, intended a multisig/timelock; never on the mint path):
- **Timelocked** (`CHANGE_DELAY = 2 days`, queue → eta → execute, each cancellable): `setVerifier`
  (guardian-set → zk swap), `returnWbisOwnership` (hand mint authority to a migrated bridge), `setGovernance`.
  A compromised governance key therefore **cannot instantly swap the trust root and mint infinitely** — the
  community has 2 days to react/exit.
- **Immediate**: `pause`/`unpause` (halts `pegInMint`; safety can't wait), and `pauseWbis`/`unpauseWbis`
  passthroughs — once the bridge is the wBIS owner, the token's own `onlyOwner` pause is reachable *only*
  through the bridge, so without these the token's emergency brake (on a token with real holders + a live pool)
  would be silently destroyed by the ownership handoff.

**Peg-out** is intentionally not on the bridge — a holder calls `wBIS.burn(amount, bismuthAddrBytes)` directly
on the token (it burns `msg.sender`); the Bismuth release contract consumes the proof of that `Burn` log.

---

## 7. The verifier — the only trust knob (`IBismuthConsensusVerifier`)

```solidity
function verifyStateRoot(bytes32 stateRoot, uint64 height, bytes calldata attestation)
    external view returns (bool);   // MUST only return true for a FINALIZED Bismuth root
```

- **`verifiers/GuardianSetVerifier.sol` — interim, deployable today (an honest M-of-N federation; labeled as
  such, NOT the endgame).** `attestation = abi.encode(bytes[] sigs)`; each is an EIP-191 signature over
  `keccak256(abi.encode(block.chainid, address(this), stateRoot, height))`; it counts **distinct** guardians in
  the set and returns `count >= threshold`. Chain-id + `address(this)` + root + height are all bound, so a
  signature can't be replayed across chains, contracts, roots, or heights; duplicate signers don't double-count.
  Guardian set + threshold are governance-timelocked. Guardians sign only finalized roots (their duty).
- **`verifiers/ZkBismuthVerifier.sol` + `interfaces/IGroth16Verifier.sol` — the endgame.**
  `verifyStateRoot` decodes `(a, b, c)` and calls `groth.verifyProof(a, b, c, [uint(stateRoot), height])`. The
  `IGroth16Verifier` is the snarkjs/gnark-generated verifier for the **Stage-3 circuit** (Bismuth PoW header
  chain + cumulative work + the committed Merkle root — doc/45 §3). The circuit/verifying-key is a frontier R&D
  dependency, not in-repo; this adapter is the drop-in seam. Swapping it in via the timelock reduces trust to
  *only the two chains' consensus* — **with no wBIS-ownership migration.**

---

## 8. Cross-chain correctness: the oracle

`bridge/evm/oracle/gen_vectors.py` → `bridge/evm/fixtures/vectors.json` is the single source of truth that pins
the Solidity to the Python consensus code. It imports `vm_merkle.py`, mirrors `vm_state`'s leaf encoding and
`bridge_vault`'s slot layout, builds a faithful multi-lock VM state (including unrelated contracts/balances so
the tree exercises promotion at odd sizes), and emits: the BLAKE2b KATs, `EMPTY_ROOT`, the `stateRoot`, and —
per lock — every slot's preimage, leaf hash, index, and inclusion proof. The Hardhat suite asserts the Solidity
reproduces all of it byte-for-byte. One of the three locks is a **leading-zero recipient**
(`0x00000000aabb…`) so the sentinel fix is exercised end-to-end (its chunks 0–1 are exactly `0x10000`).

---

## 9. Tests

**EVM (`bridge/evm/`, `npx hardhat test`) — 21 passing:**
- `Blake2b` — all 7 KATs vs `hashlib.blake2b`.
- `BismuthMerkle` — `EMPTY_ROOT` matches; every lock slot's `leafHash` + `verify(root, leaf, steps)`; tamper
  (flip sibling / flip isLeft / drop a present step) ⇒ false.
- `BismuthBridge.pegInMint` — chunk-shift+sentinel reproduces the oracle slot values; mints all three locks
  (incl. the leading-zero recipient) with the `PegInMinted` event and the right balance; reverts on replay
  (bridge nullifier *and* token `transactions`), mutated amount/recipient (bad proof), `lockId == 0`,
  `amount > MAX_LOCK`, paused, and `verifier == false`.
- `GuardianSetVerifier` — 2-of-3 passes; 1 sig fails; duplicate signer not double-counted; wrong-root fails.
- wBIS pause passthrough — governance can pause/unpause the token through the bridge; a paused token blocks
  transfers; non-governance cannot.

**Bismuth side (`python3 -m pytest tests/test_bridge_vault.py`) — 3 passing:** the real `bridge_vault` bytecode
(through `vm_engine` against a real `VMState`) records the new layout; every chunk is Merkle-provable against
the committed root; the **zero-chunk regression**; two independent locks. `tests/test_bridge_multinode.py` (the
3-node parity harness) asserts the lock amount at the new slot (`1*16`).

---

## 10. Trust model — stated exactly (do not overclaim)

| Property | Trust |
|---|---|
| mint/burn wiring, `trans` nullifier (bridge + token), BLAKE2b/Merkle inclusion, unit mapping, pause, timelock | **none** — unconditional, EVM-enforced |
| no operator key / no stored secret on the value path | by construction (authority = caller-is-owner + proof verification) |
| `IBismuthConsensusVerifier` (the *one* knob) | `GuardianSetVerifier`: honest **M-of-N** (today) → `ZkBismuthVerifier`: **only the two chains' consensus** (endgame) |
| relayers | none — liveness only; cannot forge or steal |

There is **no signer set on custody** (the Ronin/Harmony failure mode is absent by design). The attack surface
reduces to: the verifier's soundness, the verifier contracts' correctness, and the two chains' own consensus.

---

## 11. Security review (adversarial, multi-agent)

The build was authored, compiled/tested, and **adversarially reviewed across three lenses** (hash-exactness,
value-path security, token/trust), then findings were verified against ground truth before action:

- **Dismissed (false positive):** a "critical" claim that the live token uses two-step `Claimable` ownership
  (which would brick the handoff). Re-checked the verified source: it is **single-step `Ownable`**
  (`transferOwnership` sets `owner` directly; no `pendingOwner`/`claimOwnership`). The one-step handoff is
  correct; the claim was a hallucination.
- **Fixed (real, medium):** the recipient zero-stranding bug — root-caused to `vm_state` dropping `0` storage
  values and fixed with the `0x10000` sentinel chunk layout (§4.1), changed in lockstep across `bridge_vault.py`,
  both vault tests, the multinode test, the oracle, `BismuthBridge.sol`, and the spec.
- **Applied:** the wBIS pause passthrough (§6).

Attack classes checked and addressed: double-mint / replay (nullifier bound to `(vault, lockId)`, idempotent,
double-guarded by the token's `transactions`), proof forgery (leaves rebuilt from the claim — a forged
amount/recipient isn't in the committed tree), reentrancy (checks-effects-interactions + `nonReentrant`),
reorg/finality (the verifier's finality duty), guardian-signature replay (chain-id + contract + root + height
bound; distinct-signer dedup), and governance abuse (2-day timelock on every trust-affecting change).

---

## 12. Deployment / integration runbook

Per chain:
1. Deploy the Bismuth peg-in vault (`contracts/bridge_vault.py`); note its 56-hex VM address `V`.
2. Deploy a verifier — `GuardianSetVerifier(guardians, threshold, governance)` for go-live (or
   `ZkBismuthVerifier` once the Stage-3 circuit exists).
3. Deploy `BismuthBridge(wbis, verifier, vaultAddressBytes, governance)` — `vaultAddressBytes` is the **ASCII
   bytes of `V`** (56 bytes), `governance` a multisig/timelock.
4. **The non-custodial handoff:** the *current* wBIS `owner` calls `wBIS.transferOwnership(bridge)`. From that
   tx on, wBIS mints **only** via `pegInMint`. (Migrate later via the timelocked `returnWbisOwnership`.)

Per peg-in (permissionless): user `FN_LOCK`s BIS on Bismuth (≤ ~42.94 BIS/lock); after finality, anyone reads
the proofs (`vm_state.merkle_prove_storage` for the amount + 10 recipient slots) and a verifier attestation and
submits `pegInMint`. Per peg-out: `wBIS.burn(amount, bismuthAddrBytes)`.

---

## 13. Open items (dependency-blocked, not code-blocked)
- **Peg-out release contract on Bismuth** — the `eth_verify.py` signer/commitment core is built; the full MPT
  walk + sync-committee **BLS finality** need a vetted BLS12-381 dep (doc/45 Stage 1b).
- **`ZkBismuthVerifier` circuit** — the Stage-3 zk proof of Bismuth PoW consensus needs a proving toolchain
  (circom/halo2/gnark); the EVM verifier seam is ready (doc/45 Stage 3).
- **BNB wBIS interface confirmation** — verify its source matches §1 before the BSC handoff.

## 14. File inventory (`bridge/evm/`)
```
SPEC.md                                  build spec (interfaces, encodings, 0x09 ABI, trust framing)
hardhat.config.js                        solc 0.8.24, viaIR, cancun
oracle/gen_vectors.py -> fixtures/vectors.json   the cross-chain oracle
contracts/BismuthBridge.sol              the peg-in mint authority (the link)
contracts/lib/Blake2b.sol                BLAKE2b-256 via EIP-152 0x09
contracts/lib/BismuthMerkle.sol          vm_merkle inclusion verifier
contracts/verifiers/GuardianSetVerifier.sol      interim M-of-N trust root
contracts/verifiers/ZkBismuthVerifier.sol        zk endgame adapter
contracts/interfaces/{IWBIS,IBismuthConsensusVerifier,IGroth16Verifier}.sol
contracts/mocks/MockWBIS.sol             faithful token mock (tests)
contracts/test/{Blake2bHarness,MerkleHarness,StubVerifier}.sol
test/{blake2b,merkle,bridge,guardian,wbis_passthrough}.js  + test/helpers.js
```
Bismuth-side: `contracts/bridge_vault.py` (the lock layout), `vm_merkle.py` / `vm_state.py` (the commitment),
`tests/test_bridge_vault.py`, `tests/test_bridge_multinode.py`. See also [doc/45](45-bridge.md) (trust model)
and [doc/44](44-contracts.md) (the VM contract manual).
