# doc/45 — Trustless light-client / zk bridge (non-custodial cross-chain peg)

**Goal.** Let BIS move to and from other chains — and trade on their DEXs (Uniswap, etc.) as wrapped BIS —
**without a custodian, a federation, or any admin key on the value path.** Trust reduces to *the two chains'
own consensus*, nothing else.

**Non-goals.** This is not the atomic-swap path (`doc/24 §4` HTLC — a trustless *exchange* of value with a
counterparty, no wrapped asset, see also `SYS_SHA256`/`SYS_NUMBER`). It is not a federated/MPC bridge
(honest-majority custody-by-committee). Those are simpler and have their place; this doc is the *fully
unhosted* design.

**Forking.** Every consensus change below folds into the **single hf2 fork** (gated by `node.fork_height`).
The VM only executes post-fork, so new syscalls and the state-trie ride that one gate — **no second fork
signal** (see [memory: one fork hf2], `doc/18`, `doc/29`).

---

## 1. Trust model — fully unhosted, contract-based (the point of going zk over MPC)

The **security-critical parts live entirely in on-chain contracts** — no operator, no custodian:

- **Vaults / mint authority.** BIS is locked in a Bismuth VM contract; wBIS is minted/burned by an Ethereum
  contract. No company or key-holder controls the funds. Release/mint happens **only** when a contract
  verifies a valid proof.
- **Verification.** The Ethereum contract verifies Bismuth's consensus (a **zk-SNARK of Bismuth's PoW** +
  a **Merkle-trie inclusion proof** of the lock); the Bismuth VM contract verifies Ethereum's consensus
  (header chain + **MPT proof** of the burn, via the `keccak256` / `ecrecover` / BLS syscalls). All of it is
  contract code executing deterministically. Trust reduces to "the two chains' consensus," nothing else.

So: **no custodial account, no federation, no admin key on the value path** — that is the whole reason to go
light-client/zk instead of MPC.

### The one nuance: relayers (liveness, not trust)
A bridge still needs *someone* to physically carry the proof/data between chains — a **relayer**. Relayers are:
- **Permissionless** — anyone can run one; no whitelist.
- **Trustless** — they cannot forge or steal. A relayer only submits data the destination contract
  *independently re-verifies*. A lying or absent relayer cannot move funds; worst case is **delay** until an
  honest relayer (or the user themselves) submits the proof.

Relaying is an off-chain *role*, not a *host* or *custodian*. The funds and the verification are 100%
contract-based; the relayer is just a courier the contracts don't trust.

**Bottom line.** Fully unhosted, decentralized, contract-based for everything that matters (custody +
verification), with permissionless trustless relayers for liveness. The trade-off isn't trust — it's
**build effort** (the zk-consensus proof + VM crypto syscalls + Merkle trie) and a little **latency**.

| Bridge model | Trust assumption | Non-custodial? |
|---|---|---|
| Custodial (wBTC-style) | one company | ❌ |
| Federated / MPC-TSS | honest *majority* of N signers | ⚠️ custody-by-committee |
| Optimistic | 1-of-N honest watcher + challenge window | ◐ |
| **Light-client / zk (this doc)** | **only the two chains' consensus** | ✅ |
| Atomic swap (`doc/24 §4`) | none (but no wrapped asset) | ✅ (exchange, not a peg) |

---

## 2. The two-way peg

- **Peg-in (mint wBIS):** lock BIS in the Bismuth vault contract → relayer carries a proof → the **Ethereum**
  contract verifies *Bismuth* consensus (zk-PoW proof + trie inclusion of the lock) and mints wBIS.
- **Peg-out (release BIS):** burn wBIS on Ethereum → relayer carries a proof → the **Bismuth** VM contract
  verifies *Ethereum* consensus (header chain + MPT proof of the burn log) and releases the locked BIS.

The hard, irreducible requirement: **each chain must verify the other chain's consensus in-contract.** That
is what the three prerequisites below make possible.

---

## 3. Prerequisites — three stages, all hf2-gated

### Stage 1 — VM crypto syscalls (SHIPPED) ✅
The Bismuth VM can now verify Ethereum cryptography in-contract. Added to `bismuth_riscv.py` (post-fork-only
with the rest of the VM):

| # | Syscall | ABI | Gas |
|---|---|---|---|
| 15 | `SYS_KECCAK256` | `a0=ptr, a1=len, a2=out(32)` → keccak-256 of `mem[ptr:ptr+len]` to `out` | 60 |
| 16 | `SYS_ECRECOVER` | `a0=hash ptr(32), a1=sig ptr(65 = r\|s\|v), a2=out(20)` → `a0=1` + recovered 20-byte ETH address at `out`, else `a0=0` and `out` zeroed | 300 |

- keccak-256 (Ethereum's, **not** NIST sha3) over RLP / Merkle-Patricia nodes / addresses; `SYS_ECRECOVER` is
  Solidity's `ecrecover` (secp256k1 recovery + `keccak(pubkey)[12:]`) for authenticating Ethereum signers.
- Deterministic + consensus-safe: keccak via `Cryptodome` (a fixed standard), recovery via `coincurve`
  (the same secp256k1 lib shielded/ringct already use on the consensus path). Malformed input is a clean
  `a0=0`, never a leaked fault. Re-exported in `contracts/asmtools.py`; tested in `tests/test_riscv_crypto.py`
  (keccak KAT + keccak-of-calldata + ecrecover correctness/`v∈{0,1,27,28}`/rejection/determinism).
- **Verifier primitive (SHIPPED):** `contracts/eth_verify.py` — a deployable RV32I contract composing BOTH
  syscalls end-to-end: `keccak256(payload)` → `ecrecover` → authorise iff the recovered signer is the baked
  Ethereum address. This is the peg-OUT authentication core (proves the syscalls compose into real on-chain
  Ethereum verification). Tested in `tests/test_eth_verify.py` (authorised / wrong-signer / tampered-payload /
  garbage-sig / high-bit-address cases). Re-exported Asm helpers `a.keccak256()` / `a.ecrecover()`.

- **Stage 1b — `SYS_BLS_VERIFY` (DEFERRED, spec'd).** Post-Merge Ethereum *finality* is the sync committee
  (512 validators, BLS12-381). Verifying a *finalized* header needs a BLS12-381 verify syscall:
  `BLS_VERIFY(pubkey 48B (G1, minimal-pubkey-size), msg 32B root, sig 96B (G2)) -> 1/0`, a pairing check.
  **Not added this round on purpose:** a consensus primitive must be byte-identical-deterministic across
  every node, and that requires a vetted BLS12-381 dependency (`py_ecc` reference, or `blst`) — none is
  currently installed or declared in this repo. Adding the dep is the gating decision; the syscall is
  spec'd and slots in exactly like keccak/ecrecover once it lands. Until then the light client can verify
  ETH *signatures* (ecrecover) but not sync-committee *finality*.

### Stage 2 — Merkle state trie
The flat state root proves *all* state at once; a remote light-client / zk verifier instead needs a compact
proof that ONE entry (a specific locked-BIS slot) is in the committed state.

- **Stage 2a — Merkle commitment + inclusion proofs (SHIPPED).** `vm_merkle.py` — a pure, dependency-free
  binary Merkle tree (blake2b, domain-separated leaf/node tags `0x00`/`0x01`, lone-node *promotion* to dodge
  the CVE-2012-2459 duplicate-leaf collision) with O(log n) `merkle_proof` / `verify_proof`. It is defined
  over the SAME ordered entries as `state_root` (extracted into `vm_state._state_entries`, so the flat
  consensus root is **byte-identical** — commitment unchanged). `vm_state.merkle_root()` / `merkle_proof()` /
  `merkle_prove_storage()` expose it. Tested in `tests/test_vm_merkle.py` (odd-count promotion,
  tamper-rejection, and a pin that `state_root` is byte-identical post-refactor).
- **Stage 2b — Merkle root is the enforced commitment (SHIPPED).** `node.vm_state_root` — the value the
  miner embeds in the coinbase, the digester validates, the API exposes, and rollback rebuilds — is now the
  **Merkle root**, switched at all three source sites (`digest.py`, `node.py`, `chain_ops.py`) in lockstep,
  post-fork only (rides the one hf2 gate, no new signal). Inclusion proofs from `merkle_prove_storage` now
  chain to exactly what consensus commits. Validated live on regnet: committed-root enforcement
  (`test_vm_post_fork`), reorg-determinism (`test_vm_value`), and **3 independently-built ledgers commit the
  identical Merkle root** (`test_multinode_integration`). The flat `state_root()` is retained as an
  internal/debug function.
- **Stage 2c — peg-in vault (SHIPPED).** `contracts/bridge_vault.py` — `FN_LOCK` locks attached BIS naming
  an Ethereum recipient; the BIS sits in the contract's OWN custody (no operator) and the lock
  (`amount` + the 20-byte recipient) is written to storage under an incrementing id, so it is
  **Merkle-provable** against the committed root. Tested `tests/test_bridge_vault.py`: lock → custody held →
  `merkle_prove_storage` verifies against `merkle_root()` → tamper-rejection → two independent locks each
  provable. This is the custody + provable-record half of peg-in; the Ethereum-side mint (Stage 3) consumes
  exactly these proofs. (Limitation: VM callvalue/storage words are 32-bit, so a single lock's amount is
  ≤2³² units in the VM's view — widening to multi-word amounts is tracked with the rest of Stage 2/3.)

### Stage 3 — zk proof of Bismuth PoW consensus (planned, frontier)
The inbound half (Ethereum verifies Bismuth) cannot use a naive header light client: Bismuth's PoW is
**memory-hard blake2b/heavy3**, and recomputing a memory-hard hash on-chain in the EVM is gas-infeasible by
design. The trustless path is a **zk-SNARK/STARK proving Bismuth's consensus** (valid header chain +
cumulative work + the trie root) that an EVM contract verifies cheaply — a zkBridge (Succinct/Polyhedra
style). Depends on Stage 2 (the trie root is what the proof commits to). The heaviest, most R&D-bound piece.

---

## 4. Security

Bridges are the single most-exploited component in crypto (>$2B lost). Here the attack surface is, by design,
*only*: (a) soundness of the zk-consensus proof system, (b) correctness of the verifier contracts on both
chains, (c) the two chains' own consensus (51%/finality assumptions). There is **no signer set to compromise**
(the Ronin/Harmony failure mode). Standard guards apply and are designed-in from day one (per the standing
attack-vector rule, `doc/25`): double-mint / replay (each lock/burn consumes a unique nullifier), reorg
safety (require N confirmations / finality before mint), proof malleability, and trie-root staleness.

## 5. Status

| Stage | Component | Status |
|---|---|---|
| 1 | `SYS_KECCAK256` / `SYS_ECRECOVER` VM syscalls | **done** — `bismuth_riscv.py`, `tests/test_riscv_crypto.py` |
| 1b | `SYS_BLS_VERIFY` (ETH sync-committee finality) | **deferred** — needs a BLS12-381 dep (`py_ecc`/`blst`); spec'd, not added |
| 1c | `eth_verify.py` verifier contract (syscalls compose) | **done** — `tests/test_eth_verify.py` |
| 2a | Merkle commitment + inclusion proofs (`vm_merkle.py`) | **done** — `tests/test_vm_merkle.py` |
| 2b | Merkle root as the enforced (hf2-gated) commitment | **done** — `digest.py`/`node.py`/`chain_ops.py`; live regnet + 3-node validated |
| 2c | peg-in vault `bridge_vault.py` (custody + provable lock) | **done** — `tests/test_bridge_vault.py` |
| 3 | zk-SNARK of Bismuth PoW consensus (EVM-verifiable) | planned (frontier — needs a proving stack) |
| — | peg-out MPT/finality verifier on Bismuth | partial — `eth_verify.py` (signer) done; MPT walk + finality (1b) pending |
| — | wBIS verifier + mint/burn contracts (Solidity, Ethereum) | design (§9) — depends on Stage 3 |
| — | permissionless relayer reference client | design (§10) |

---

## 6. Live wBIS deployments (reference)

Wrapped BIS (`wBIS`) is already live and trading on Ethereum and BNB Chain. The trustless bridge in this
doc is the path to backing `wBIS` **non-custodially** (Stages 1–3 above); these are the current on-chain
addresses to integrate against / point the bridge's mint-burn contracts at.

| Chain | Role | Address | Explorer |
|---|---|---|---|
| Ethereum | `wBIS` ERC-20 token | `0xf5cb350b40726b5bcf170d12e162b6193b291b41` | https://etherscan.io/token/0xf5cb350b40726b5bcf170d12e162b6193b291b41 |
| Ethereum | ETH / wBIS pool (Uniswap) | `0xf4f82f8d84c529987201609cecee8ab136a50c8c` | https://app.uniswap.org/explore/pools/ethereum/0xf4f82f8d84c529987201609cecee8ab136a50c8c |
| BNB Chain | `wBIS` BEP-20 token | `0x56672ecb506301b1e32ed28552797037c54d36a9` | https://bscscan.com/token/0x56672ecb506301b1e32ed28552797037c54d36a9 |
| BNB Chain | BNB / wBIS pool | `0x731b8244f818fd488d9dc516edd976a96459ae59` | https://bscscan.com/address/0x731b8244f818fd488d9dc516edd976a96459ae59 |

> Addresses are lowercase as supplied; verify the EIP-55 checksum before hard-coding into a contract.

---

## 7. Component inventory (built vs designed)

| Where | Component | File | State |
|---|---|---|---|
| Bismuth VM | `keccak256` / `ecrecover` syscalls | `bismuth_riscv.py` | ✅ built + tested |
| Bismuth VM | `bls_verify` syscall (ETH finality) | — | ⏸ spec'd, dep-blocked (§3 Stage 1b) |
| Bismuth state | Merkle commitment + inclusion proofs | `vm_merkle.py`, `vm_state.py` | ✅ built + tested |
| Bismuth consensus | Merkle root = committed state root | `digest.py`/`node.py`/`chain_ops.py` | ✅ built + live-validated |
| Bismuth contract | peg-in **vault** (lock BIS, provable record) | `contracts/bridge_vault.py` | ✅ built + tested |
| Bismuth contract | ETH-signer **verifier** (peg-out auth core) | `contracts/eth_verify.py` | ✅ built + tested |
| Bismuth contract | peg-out **release** (verify ETH burn → release) | — | ◐ partial — signer done; MPT walk + finality pending |
| Ethereum | `wBIS` ERC-20 + Uniswap pool | live (§6) | ✅ deployed (today's wBIS) |
| Ethereum | Bismuth-consensus **zk verifier** + mint/burn | — | 📐 design (§9); needs Stage 3 proof |
| off-chain | permissionless **relayer** | — | 📐 design (§10) |

Everything in the *value path* that is buildable without a new dependency or a proving stack is **built and
tested**. The two remaining gaps are exactly the two environment-blocked pieces: a BLS12-381 dep (Stage 1b)
and a zk proving toolchain (Stage 3).

## 8. End-to-end flows

**Peg-in (BIS → wBIS), trustless:**
1. User calls `bridge_vault.FN_LOCK(eth_recipient)` on Bismuth, attaching the BIS to lock. The BIS enters the
   vault's own custody; the lock `(amount, eth_recipient, id)` is written to consensus state.
2. After the block is mined, the lock is a leaf under the committed **Merkle state root** — provable with
   `vm_state.merkle_prove_storage` (`§Stage 2`).
3. A **relayer** (anyone) produces a **zk proof** that "Bismuth committed this state root at a finalized
   height" (`§Stage 3`) and the Merkle inclusion proof of the lock, and submits both to the Ethereum verifier.
4. The Ethereum verifier contract checks the zk proof + inclusion proof (no trust in the relayer) and **mints
   `amount` wBIS to `eth_recipient`**. Each lock id is a nullifier → no double-mint.

**Peg-out (wBIS → BIS), trustless:**
1. User **burns** wBIS on Ethereum, naming a Bismuth recipient; the burn emits a log committed under the
   block's `receiptsRoot`.
2. A relayer carries the Ethereum header + a Merkle-Patricia proof of the burn log to the Bismuth release
   contract.
3. The Bismuth contract verifies: the header is **finalized** (sync-committee BLS — Stage 1b) and the burn log
   is included under its `receiptsRoot` (RLP + MPT walk hashed with `keccak256` — `eth_verify.py` proves the
   signer/commitment core today; the full MPT walk is the remaining piece), then **releases the locked BIS**.
   Each burn is a nullifier → no double-release.

Neither flow has a custodian or a signer set: step 3/4 is contract code re-verifying the *other chain's
consensus*. Relayers only carry data the destination independently re-checks (`§1`).

## 9. Ethereum side (design)

A Solidity verifier + the wBIS mint/burn authority (replacing today's wBIS minter with a trustless one):
- `verifyBismuthState(zkProof, stateRoot, height)` — a Groth16/Plonk verifier (auto-generated from the
  Stage-3 circuit) attesting the root was committed at a finalized Bismuth height.
- `mint(inclusionProof, lockId, recipient, amount, stateRoot)` — checks `verifyBismuthState` + the Merkle
  inclusion of the lock leaf (the same `vm_merkle` scheme, re-implemented in Solidity: blake2b + the
  `0x00/0x01` domain tags + promotion rule), marks `lockId` consumed, mints wBIS.
- `burn(amount, bismuthRecipient)` — burns wBIS and emits `Burn(bismuthRecipient, amount, nonce)` for the
  peg-out proof.
The blake2b + Merkle verify in Solidity is cheap; the zk verify is one pairing-check precompile call.

## 10. Relayer (design)

A stateless, **permissionless** daemon (anyone runs one; reference client to ship): watch both chains, build
the zk + inclusion proofs for pending locks/burns, submit to the destination verifier, retry/replace.
It holds no keys to user funds and cannot forge a proof — a wrong or absent relayer only delays a transfer
until an honest one (or the user) submits. Liveness, not trust (`§1`).

## 11. How to finish (the two dependency decisions)

1. **Stage 1b — add a BLS12-381 dependency** (`py_ecc` reference or `blst`), then implement `SYS_BLS_VERIFY`
   exactly as keccak/ecrecover were added (the ABI is fixed in §3). Unblocks ETH sync-committee finality →
   the peg-out release contract.
2. **Stage 3 — stand up a zk proving toolchain** (circom/halo2/gnark) and build the circuit proving Bismuth's
   header chain + cumulative blake2b/heavy3 work + the committed Merkle root, with an EVM verifier. Unblocks
   the peg-in mint.
Both are environment/dependency decisions, not code that can be conjured deterministically here; everything
they depend on (the VM crypto, the provable Merkle state, the vault, the signer verifier) is already built
and tested.
