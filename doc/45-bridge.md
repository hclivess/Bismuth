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
| 3 | zk-SNARK of Bismuth PoW consensus (EVM-verifiable) | planned (frontier) |
| — | Vault + wBIS verifier contracts, relayer reference client | planned (build on 1–3) |

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
