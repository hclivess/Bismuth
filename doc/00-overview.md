# 00 — Modernization overview (start here)

The single map of the Bismuth node modernization: what exists, what's active, what's deliberately
inert, and what's planned. Detailed companions: file index [`13`](13-file-reference.md), DB deep-dive
[`16`](16-database-rework-plan.md), roadmap [`17`](17-roadmap.md), the hard fork [`18`](18-hardfork-hf2.md),
the VM [`19`](19-vm.md), post-quantum signatures [`20`](20-post-quantum.md), the solo miner / PoW
[`21`](21-mining.md).

## Principles (non-negotiable)

1. **Consensus does not change** except at an explicit, signalled hard fork. The signing-buffer and
   block-hash byte forms are frozen in `bismuth_serialize.py` and characterization-locked.
2. **Replay-verified.** Every storage/representation change re-hashes the whole chain through the frozen
   boundary (`replay_verify.py`, `tests/test_replay.py`) and must be **byte-identical**.
3. **Incremental, reversible, default-off.** New behaviour is config-flagged and validated on regnet +
   independent replay before it can matter. Old peers keep working.
4. **Modernize at the edges, freeze the core.** New clean modules at the seams; the consensus core is
   touched only through the frozen layer, and only at the fork.

## Current state — honest status table

| Area | What | State |
|---|---|---|
| Consensus-safety guardrail | `tests/test_consensus_invariants.py` — no overspend / double-spend / negative / foreign-sender / tampered-sig | **active, every test run** |
| Integer atomic-unit storage | `amounts.py` + `replay_verify` + `migrate_amounts` | **on (regnet)**, default-off mainnet |
| LMDB block store + pubkey dedup | `block_store.py` — lossless, content-addressed, 1.73 GB for 1M blocks (~2× under SQLite) | **integrated as shadow** behind `block_store` flag; SQLite still primary |
| Balance index (O(1) balances) | `balance_index.py` — bit-matches `ledger_balance3` | **built + validated**, not wired |
| Reward sidechain | `reward_chain.py` — retires negative-height mirror rows, balance-preserving | **built + validated**, not wired |
| Auto hard-fork scheduler | `fork.dynamic_fork_height` + coinbase signal writer + `/api/fork` | **WIRED** — signal→detect→schedule works end-to-end on regnet (`test_fork_wiring`); inert on mainnet until miners signal |
| LWMA difficulty | `difficulty_lwma.py`, gated in `difficulty()` | **GATED behind `fork_height`** (LWMA past activation, legacy before); inert until a fork activates |
| GPU miner | `gpuminer/` — CUDA (kbkminer) + OpenCL, today's Heavy3 | **vendored**, GPU-untested here |
| REST API | `rest_api.py` — status/blocks/balance/tx/headers/peers/`fork` | **active** on the live node (:5659) |
| Address-history query | composite indexes (migration v2) + UNION rewrite | **done + LIVE** — 2.5 s → 0.06 s |
| Bitcoin JSON-RPC | `rpc_bitcoin.py` — getblockcount/getblock/getbalance/getrawtransaction/… | **implemented**, flag `rpc_bitcoin` (off); regnet-tested |
| Ethereum/ERC shim | `rpc_ethereum.py` — `eth_*` subset (bounded; not an EVM) | **implemented**, flag `rpc_ethereum` (off); regnet-tested |
| Block explorer | `explorer.bismuth.cz` (SPA over the REST API) | **live** — blocks/tx/address + **Tokens / Nodes / Supply / Contracts** views, SVG favicon, node-switcher |
| Explorer/RPC endpoints | `/api/supply`, `/api/tokens`, `/api/token/{n}`, `/api/nodes`, `/api/vm/*` + token-index | **live/done** (supply background-computed; token-first indexes) |
| Bootstrap hosting + snapshot | `https://bismuth.cz/ledger.tar.gz` + `scripts/snapshot.py` | **live** — live-safe (SQLite online-backup + LMDB `env.copy`), integrity-checked |
| Balance index | `balance_index.py` — O(1) display balance | **WIRED** (flag `balance_index`): maintained on commit, reorg-rebuilt, read by `/api/balance`; consensus stays on `ledger_balance3` |
| Peer reputation + penalization | `peers_reputation.py` | **WIRED** — validate-height-is-real reward/penalize (synced-only), reputation-weighted tip |
| Auto-recovery rollback | `essentials.rollback_allowed` | **default ON** (`rollback_consensus`) — reputation-gated deep rollback replaces the rigid `rollback_depth` strand |
| Unified rollback + reorg test | `chain_ops._rollback_aux_stores` | **done** — ledger + all stores roll back in sync (`test_rollback_reorg`) |
| **Decentralized-apps VM** | `bismuth_riscv.py` (RV32I) + `vm_state`/`vm_engine` | **built + regnet-tested, POST-FORK + flag** — deploy/call, a single RISC-V engine, ENFORCED state root, value custody (contracts move real BIS), HTLC. **See [doc/19](19-vm.md)** for the full status |
| Connectivity/sync fixes | self-dial false-consensus, back-off, headers-first, ed25519 dep | **active** |

"Shadow" = written/maintained alongside the authoritative store but not yet read from. "Inert" =
present and tested but never called by the running node.

## How the pieces fit

**Storage (phase 7).** Immutable block bodies → `block_store.py` (LMDB, append-only, pubkey-deduped).
Queryable balances → `balance_index.py` (O(1), integer units). Locally-minted rewards →
`reward_chain.py` (out of the negative-height mirror rows). All sit *behind the frozen boundary*, so
they change no block hash — proven lossless / bit-matching / replay-identical. The block store is wired
into the digester as an **additive shadow write** (`block_store=True`); the others are built and
validated but not yet wired. Cutover (make LMDB the read path) is the remaining storage step.

**Consensus safety.** `tests/test_consensus_invariants.py` enforces the value rules on every run; it is
the robustness guardrail every future change must keep green. The one structural bug fixed live was a
node **dialing itself and reporting false 100 % consensus** (`peers_pool.can_connect_to`).

**The hard fork (`hf2`, doc/18).** Everything that changes the block hash is bundled into one
deliberately-scheduled event, activated by `fork.dynamic_fork_height`: upgraded miners stamp a signal
into their coinbase, every node computes the activation height identically from the chain (no split),
and the new rules switch on at the next round-1000 boundary. Bundle: integer/binary serialization +
content-hash txid + canonical sig/pubkey encoding; the reward-sidechain cutover; and the **LWMA
difficulty** (symmetric, delicate, deterministically calculable — the fix for the brutal up-only
ratchet); and the **blake2b Heavy3 PoW swap** (bundled into the same single fork since 2026-06-12 —
stamping `hf2` asserts blake2b mining readiness too). **Continuity:** old blocks keep their bytes and
hashes; new rules apply forward only; validation is height-gated; no re-sync. The gate scaffold now
exists: the **scheduler is wired** (`fork.dynamic_fork_height`, cached on the single `node.fork_height`
in `digest.py`) and **LWMA is gated** in `difficulty.py` (active past activation, legacy before); only
the **reward sidechain** remains an inert module. The **dual-algo PoW** (today's sha224-inner Heavy3
vs. the modernised blake2b-inner Heavy3, same anneal + substring difficulty, selected by
`height >= node.fork_height`) is also **built** (`mining_heavy3.py` `new_pow` switch, gated in
`digest.py`/`miner.py`) and inert until miners signal hf2 — see
[21](21-mining.md).

**Mining.** Today's PoW is Heavy3 (`mining_heavy3.py`: sha224 → 1 GB memory-hard anneal → substring
difficulty). `gpuminer/` mines it on GPU. The miner is coupled to the PoW: any Heavy3 change must update
the kernels in lockstep.

**Edges.** `rest_api.py` (+ `rest_client.py`, `transport.py`, `api_sync.py`) is the modern, parallel,
compressed alternative to the legacy socket stack; `explorer.bismuth.cz` consumes it. Planned edge
adapters: Bitcoin-compatible JSON-RPC and an Ethereum/ERC compatibility shim (doc/17).

## Operational deployment (live)

- **Node:** synced mainnet node, `rest_api=True` on :5659, serving the API.
- **bismuth.cz:** nginx + Let's Encrypt; serves the **bootstrap snapshot** (`ledger.tar.gz`, at-tip,
  produced with zero downtime by `_mkbootstrap.sh`) and links the explorer.
- **explorer.bismuth.cz:** static SPA + same-origin `/api/` proxy to the node; block paging, address +
  tx + balance lookup.
- The node's historical default `bootstrap_url` is `https://bismuth.cz/ledger.tar.gz`, so existing
  nodes bootstrap unchanged.

## Testing

`python3 -m pytest -q` boots a real regnet node and runs the full suite (consensus invariants, replay
byte-identity, characterization lock, storage round-trips/bit-match, headers sync, REST, mempool
anti-spam, the LWMA + fork-scheduler unit tests, …). The storage tests run with `block_store=True`, so
the suite green *with the shadow store active* is itself the proof it's mining-invariant.

## What's left (in rough order)

1. ~~**Wire the auto-fork switch:** coinbase signal writer + `dynamic_fork_height` called/cached +
   `/api/fork` readiness view + the `block_height >= fork_height` gate scaffold in `digest`.~~ **DONE** —
   signal writer in `miner.py`, `dynamic_fork_height` cached on `node.fork_height` in `digest.py`,
   `/api/fork`, and the height gate in the digester are all in place.
2. **Gate the remaining consensus upgrades** behind it: the reward-sidechain cutover, then the
   serialization/txid/sig-pubkey changes — each replay-validated. (LWMA difficulty is already gated.)
3. **Storage read-path cutover** (LMDB primary), then mainnet integer-amount cutover.
4. **Edge adapters:** Bitcoin JSON-RPC, ETH/ERC shim.
5. **Agreement hardening** (optional): enable/harden `rollback_consensus`, checkpoints.
6. **Heavy3/PoW change** (optional, separate fork) + the matching GPU-miner kernel update.
