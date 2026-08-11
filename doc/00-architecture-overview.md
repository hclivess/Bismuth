# 00 — Architecture overview (start here)

A navigable, top-level map of the Bismuth full node **as it stands today**, with an honest
shipped-vs-deferred status for every subsystem and a one-line pointer to every other doc in this
directory. If you are new, read this, then [`01`](01-overview.md) (what Bismuth is) and
[`02`](02-architecture-and-threads.md) (startup + threads).

> Two other "start here" docs exist and are intentionally kept: [`00-overview.md`](00-overview.md) is
> the **modernization** status table (what is shadow/inert/wired), and [`01-overview.md`](01-overview.md)
> is the conceptual intro. This file is the **architecture map** — the subsystem topology and where each
> doc lives.

---

## 1. The shape of the node

Bismuth is a single multithreaded Python process — `node.py` — that is full node, miner, wallet
server, and API server at once. Transactions are 8-field tuples (`timestamp, sender, recipient,
amount, signature, public_key, operation, openfield`); the `operation`/`openfield` pair is a generic
data channel that features (tokens, aliases, shielded value, VM contracts) ride on top of without
changing the base tuple. PoW is memory-hard "Heavy3"; signatures default to RSA-4096 via the vendored
`polysign` abstraction. Networks: mainnet (`5658`, `static/ledger.db`), testnet (`2829`), regnet
(`3030`, mint-on-demand). See [`01`](01-overview.md), [`02`](02-architecture-and-threads.md).

```
                         ┌──────────────────────── node.py ────────────────────────┐
  peers (TCP 5658) ────► │ ThreadedTCPServer → per-conn handler (socket command set)│
  wallets/explorers ───► │           │                                              │
  (legacy socket API)    │           ├─ digest_block()  ── consensus core ──────────│
                         │           │     ├ mining_heavy3 (PoW verify)             │
  REST clients ────────► │ rest_api  │     ├ difficulty / difficulty_lwma (retarget)│
  (HTTP :5659, opt-in)   │ (aiohttp) │     ├ polysign / SignerFactory (sig verify)  │
                         │           │     ├ DbHandler (SQLite trio + index)        │
                         │           │     └ LMDB stores (shadow/projections)       │
                         │  Mempool ◄┘     vm_engine · ShieldedState · token_index  │
                         │  ConnectionManager → worker() threads → outbound sync     │
                         └───────────────────────────────────────────────────────────┘
```

The consensus pipeline is `digest.py` (`digest_block`): take `db_lock`, validate every tx
(signature → balance/overspend → double-spend), verify PoW against current difficulty, compute the
reward, write the block, prune the mempool, advance the tip. See [`03`](03-consensus-blocks-digest.md).

---

## 2. Subsystem map (honest status)

### Storage — SQLite trio + LMDB projections + the engine-agnostic KVStore seam
**State: hybrid. SQLite is still canonical for consensus reads + the lockstep write path; LMDB
projections are proven and run in parity-strict shadow on regnet; full cutover is deferred.**

- **The legacy "H1/H2/H3" SQLite trio** (`dbhandler.py`): `ledger.db` (full history), `hyper.db`
  (hyperblock roll-ups + recent blocks), a mode-dependent working cursor, plus `index.db`
  (tokens/aliases) and `mempool.db`. This is the live 23 GB prod store and remains **authoritative**.
  The two consensus reads that still hit SQLite are the overspend check (`ledger_balance3`) and the
  duplicate-signature replay scan. See [`05`](05-database-and-ledger.md), [`16`](16-database-rework-plan.md).
- **LMDB stores (8)** — each a rebuildable, height-keyed, reorg-safe projection or the content-addressed
  block store: `block_store.py` (canonical-target block bodies, pubkey-deduped), `balance_index.py`
  (O(1) balances), `txid_index.py` (content-txid → height, the malleability-tight dedup key),
  `reward_chain.py` (locally-minted rewards, retires negative-height mirror rows), `shieldedv1.py`
  (notes/key-images/flows), `vm_state.py` (contract code/storage/custody balances), `token_index.py`
  (tokens + alias-evolution side-index), and the snapshot/scripts store. **Shielded and token/alias
  storage are fully migrated off SQLite** (doc/26 stages 1–2, shipped). Block store + balance index +
  txid index run in **parity-strict shadow/primary** on the regnet proving suite (proven
  consensus-faithful), default **off** on mainnet so the byte output is identical.
- **The KVStore engine seam** ([`36`](36-kvstore-engine-seam.md), `kvstore.py`): one small interface +
  an `open_store(backend, path, dbs=...)` factory so the KV engine is a single-arg choice
  (`lmdb` | `mdbx` | `sqlite-kv`). **All 8 LMDB stores were migrated onto it** (each now calls
  `open_store` instead of `lmdb.open()` directly — confirmed in `block_store/balance_index/vm_state/
  reward_chain/token_index/shieldedv1/txid_index`). A centralized `Codec` (msgpack with the same JSON
  fallback the stores used) keeps the on-disk format byte-identical so the existing parity tests still
  hold; the LMDB wrapper is a thin passthrough (measured few-% overhead). `sqlite-kv` is always
  available (stdlib); `mdbx` is imported lazily.
- **The SQLite → LMDB migration** ([`26`](26-storage-postfork.md)): stages 1–4 foundations shipped
  (LMDB shielded + token/alias stores; read seam + REST/socket block-read migration; write seam as
  additive shadow; balance + txid parity bake-ins proven in `primary`). **Deferred:** flip the
  canonical write to LMDB and retire the `commit_marker`/`ATTACH` lockstep, sync-without-hyperblocks,
  and deleting the trio — these need a two-node + crash-recovery harness. Endgame: single LMDB store
  canonical, **post-fork only**; legacy SQLite keeps working pre-fork for old peers.

### Consensus, digest, difficulty + the #23 self-heal
**State: live core unchanged; difficulty self-heal autoheal-on by default, soaked on regnet.**

- **Digest/validation** (`digest.py`, `digest_tx.py`, `chain_ops.py`): the authoritative validate +
  commit pipeline; reorg/rollback rolls back the ledger and **all** aux stores in sync
  (`chain_ops._rollback_aux_stores`). See [`03`](03-consensus-blocks-digest.md).
- **Difficulty controller** (`difficulty.py`): a PID-style retarget recursive on the *stored* previous
  value, 60 s target. The fork-gated **LWMA** retarget (`difficulty_lwma.py`) replaces its output past
  `node.fork_height` (`difficulty.py:106-112`); inert until a fork activates. See [`04`](04-pow-and-difficulty.md).
- **#23 difficulty-divergence detector + guarded self-heal** ([`35`](35-difficulty-divergence-selfheal.md)):
  fixes a self-reinforcing corrupted-`misc` fixed point (a node mining at the wrong difficulty). A
  300 s daemon polls height-matched peers' `/api/difficulty`, takes the median, and on a debounced,
  supermajority-confirmed divergence heals by writing a clamped `rollback_to` target and restarting so
  `difficulty()` re-derives from a clean base. **`diff_divergence_detect` and `diff_divergence_autoheal`
  are both default True** (autoheal enabled after the regnet soak validated detection); hard loop guards
  (permanent per-ledger lifetime cap → advisory-only, cooldown, bounded depth, `rollback_allowed`
  anti-sybil) make it self-limiting. Core logic is `chain_ops.detect_difficulty_divergence`.
- **The Accuser** ([`31`](31-accuser.md)): detects/proves/propagates miner equivocation and feeds
  finality substitutes (checkpoints, reorg caps, reputation). **Phase 1 inert-by-default + gossip-gated;**
  on-chain slashing + a non-forgeable proof are Phase 2, folding into hf2.
- **From-genesis sync exceptions** ([`30`](30-genesis-sync-exceptions.md)): a trusted
  `assume_valid_height` + a mainnet-only, default-inert per-height waiver registry for the handful of
  historical manual-intervention blocks. **Built, default-inert.**

### Networking, REST API, api_sync
**State: legacy socket stack live; REST live on the node; api_sync consumer deferred (no REST peers yet).**

- **Legacy P2P** (`connections.py`, `connectionmanager.py`, `peershandler.py`, `worker.py`): a custom
  10-byte length header + JSON over raw TCP, thread-per-connection; consensus = the most-common peer
  height. Being superseded by the API but not off-limits to modularization (only `node.py handle()` is
  deferred). See [`06`](06-networking-protocol.md), [`07`](07-mempool.md) (mempool), [`08`](08-api-and-commands.md)
  (socket command + CLI catalog).
- **Modern REST API** (`rest_api.py`, `rest_client.py`, `transport.py`): parallel, **on by default**
  (`rest_api=True`; set False to opt out), CORS, compressed; status/blocks/balance/tx/headers/peers/fork/difficulty/supply/
  tokens/vm/etc. **Live on the production node (:5659)**; `explorer.bismuth.cz` consumes it. Bitcoin
  JSON-RPC (`rpc_bitcoin.py`) and an ETH/ERC shim (`rpc_ethereum.py`) are implemented but flag-off.
  See [`15`](15-rest-api.md).
- **api_sync** (`api_sync.py`, `api_sync_worker.py`): headers-first, capability-gated chain-segment
  fetch over REST — the safe seam returning digester-ready blocks (validates transport; consensus stays
  in the digester). **Two-node harness proves a node reconstructs a peer's whole chain over REST alone**,
  but it is behind the default-off `api_sync` flag and **no live mainnet peer is REST-capable yet**, so
  on mainnet the node only *serves* today.

### The VM + dApp demos
**State: REMOVED.** The decentralized-apps VM (RV32I engine, contract state store, custody payouts) and
every dApp built on it were deleted — the node never activated them and they are not coming back. `vm:`
operations, if any appear on chain, are stored as ordinary inert transaction data and never execute.

### Shielded value
**State: stages 1–3 implemented, gated on hf2 (inert until then).**

`shieldedv1.py` (stealth addresses + ring signatures with key images), `ringct.py` + `bulletproof.py`
(stage 3 confidential amounts: Pedersen commitments + aggregated bit range proofs + 2-column MLSAG on
secp256k1). Storage is on LMDB (doc/26 stage 1). See [`22`](22-shielded.md), audit in [`25`](25-security-audit.md).

### Tokens / aliases plugin
**State: shipped as an optional plugin, config-gated, owns its own LMDB store (inert on mainnet pre-fork).**

Tokens and aliases are out of node core into `plugins/tokens_aliases` (no SQLite, owns its LMDB store
via `token_index.py`); core defers to it. Gated by the `token_index` flag (default off; on in the
regnet test config). Aliases gained mutable ownership (register/transfer/free). See [`27`](27-plugins.md),
storage in [`26`](26-storage-postfork.md) §2.

### Browser wallet / injected provider
**State: design + reusable provider shipped for the dApp demos; non-custodial.**

A reusable `window.bismuth` provider + non-custodial browser wallet (RSA-SHA1 tx signing with a JS↔Python
cross-check harness), replacing the server-side `relay.py` signing the dApp SPAs use today. See
[`33`](33-browser-wallet.md).

### The hf2 fork gate
**State: scheduler WIRED end-to-end on regnet; inert on mainnet until miners signal.**

There is **exactly one** fork — `hf2` — and everything that changes consensus folds into it (user
directive: "everything will be one fork"). Activation is signal-driven: upgraded miners stamp a coinbase
signal, every node computes the same activation height from the chain (`fork.dynamic_fork_height`, cached
on the single `node.fork_height`), and the new rules switch on at the next round boundary — **never add a
second fork signal**. The bundle: binary/integer serialization + content-hash txid (also the VM contract
address, and the signature now signs the txid) + raw-byte sig/pubkey + pubkey-by-reference + coinbase
compaction (the freed coinbase slots repurposed as a free-form mining header, [`41`](41-hf2-coinbase-free-fields.md));
LWMA difficulty; blake2b Heavy3 PoW swap; VM mandatory state-root commitment; shielded/RingCT;
native multisig senders. Serialization Stage 0–2 are live-but-dormant; later stages staged. See
[`18`](18-hardfork-hf2.md) and the authoritative serialization spec [`29`](29-hf2-serialization-v2.md).
Operational gate: the GPU miner kernels (`gpuminer/`) are sha224-only and must be ported to blake2b
before any mainnet hf2 signal ([`21`](21-mining.md)).

### Crypto / signatures
RSA-4096 default via vendored `polysign` (ECDSA secp256k1/secp256r1, ED25519, ML-DSA-44/65/87). Verification
routes through `SignerFactory.verify_tx_signature` (fork-aware). See [`09`](09-crypto-wallets-keys.md);
post-quantum pivot held in reserve in [`20`](20-post-quantum.md); HD/BIP32-39 + multisig in [`23`](23-hd-multisig.md).

---

## 3. Pointer to every doc

| Doc | Topic |
|---|---|
| [00-architecture-overview.md](00-architecture-overview.md) | **This file** — architecture map + pointer to every doc |
| [00-overview.md](00-overview.md) | Modernization status table (shadow / inert / wired) |
| [01-overview.md](01-overview.md) | What Bismuth is: networks, tx model, high-level architecture, entry points |
| [02-architecture-and-threads.md](02-architecture-and-threads.md) | Node startup sequence, threading model, locks, shared state |
| [03-consensus-blocks-digest.md](03-consensus-blocks-digest.md) | Block digestion pipeline, tx validation, rewards/fees, ledger schema |
| [04-pow-and-difficulty.md](04-pow-and-difficulty.md) | Heavy3 PoW, HMAC-DRBG lookup file, difficulty retarget, LWMA gate |
| [05-database-and-ledger.md](05-database-and-ledger.md) | `DbHandler`, the SQLite databases & schemas, RAM/hyper modes, quantizer |
| [06-networking-protocol.md](06-networking-protocol.md) | Wire protocol, P2P command catalog, worker lifecycle, peers/ban logic |
| [07-mempool.md](07-mempool.md) | Mempool schema, acceptance rules, `merge()`, size tiers & fees, locking |
| [08-api-and-commands.md](08-api-and-commands.md) | Legacy socket API (`api_*`), core commands, `commands.py` CLI |
| [09-crypto-wallets-keys.md](09-crypto-wallets-keys.md) | Address/key derivation, signing/verification, polysign, fee formula |
| [10-features.md](10-features.md) | Tokens, staking PoC, plugin hooks, regnet (feature layers) |
| [11-configuration.md](11-configuration.md) | `config.txt` reference (every key), mandatory message, peers files |
| [12-tooling-build-tests.md](12-tooling-build-tests.md) | Send/balance scripts, test suite, snapshot tooling, build & CI |
| [13-file-reference.md](13-file-reference.md) | Accurate per-file index of the whole tree |
| [14-known-issues-and-improvements.md](14-known-issues-and-improvements.md) | Verified bugs, fragile wiring, refactor/upgrade list |
| [15-rest-api.md](15-rest-api.md) | The modern, parallel REST/JSON API (**on by default**, reads only; writes need `rest_api_write`) |
| [16-database-rework-plan.md](16-database-rework-plan.md) | Storage-layer modernization design/roadmap (the deep-dive) |
| [17-roadmap.md](17-roadmap.md) | Modernization roadmap: phases, shipped vs planned, refactor history |
| [18-hardfork-hf2.md](18-hardfork-hf2.md) | The bundled `hf2` fork: signal-activated scheduler, the consensus change set |
| [20-post-quantum.md](20-post-quantum.md) | Post-quantum signatures: ML-DSA signer, the PQ fork path (held in reserve) |
| [21-mining.md](21-mining.md) | The built-in solo miner (`miner.py`) and the dual-algo PoW signalling |
| [22-shielded.md](22-shielded.md) | Shielded value: stealth addresses, ring signatures, RingCT confidential amounts |
| [23-hd-multisig.md](23-hd-multisig.md) | BIP32/BIP39 HD wallets and M-of-N multisig (native signer + VM vault) |
| [25-security-audit.md](25-security-audit.md) | Adversarial security audit: findings, fixes, regression tests |
| [26-storage-postfork.md](26-storage-postfork.md) | Post-fork storage rearchitecture: retiring SQLite for one LMDB store |
| [27-plugins.md](27-plugins.md) | Modern plugin framework; the `tokens_aliases` plugin (tokens/aliases out of core) |
| [29-hf2-serialization-v2.md](29-hf2-serialization-v2.md) | hf2 binary/integer serialization (authoritative spec) |
| [30-genesis-sync-exceptions.md](30-genesis-sync-exceptions.md) | From-genesis sync & historical validation-exception registry |
| [31-accuser.md](31-accuser.md) | The Accuser: detect/prove/propagate miner equivocation (finality substitutes) |
| [33-browser-wallet.md](33-browser-wallet.md) | Browser wallet + injected `window.bismuth` provider (non-custodial) |
| [35-difficulty-divergence-selfheal.md](35-difficulty-divergence-selfheal.md) | #23 difficulty-divergence detector + guarded self-heal |
| [36-kvstore-engine-seam.md](36-kvstore-engine-seam.md) | Engine-agnostic KV store seam (`kvstore.py`, `open_store` factory; all 8 LMDB stores migrated) |
| [CHANGELOG-2026-06.md](CHANGELOG-2026-06.md) | Commit-anchored engineering changelog for the 2026-06 session (poker, #23 self-heal, KVStore seam) |
| [README.md](README.md) | Doc-set intro + index (note: index currently lists 00–28 only) |

> **Index gap:** `README.md`'s index stops at doc 28, so docs 29–36 and the changelog are not yet
> linked there — this file ([`00-architecture-overview.md`](00-architecture-overview.md)) is the
> complete, current index. The engine-agnostic **KVStore DB seam** (`kvstore.py`) now has its own
> page at [`36`](36-kvstore-engine-seam.md) (it is also referenced from each store's header and from
> doc/26 "storage stage 1").

---

## 4. The non-negotiable principles

1. **Consensus does not change except at the one signalled `hf2` fork.** The block-hash and signing
   pre-images are frozen in `bismuth_serialize.py` and characterization-locked.
2. **Replay-verified.** Every storage/representation change re-hashes the chain through the frozen
   boundary and must be byte-identical (`replay_verify.py`, `tests/test_replay.py`).
3. **Incremental, reversible, default-off.** New behaviour is config-flagged, validated on regnet +
   replay before it can matter; old peers keep working.
4. **Modernize at the edges, freeze the core.** New clean modules at the seams; the consensus core is
   touched only through the frozen layer, only at the fork.
