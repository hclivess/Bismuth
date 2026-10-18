# 00 — Architecture overview (start here)

A navigable, top-level map of the Bismuth full node **as it stands today**, with an honest
shipped-vs-deferred status for every subsystem and a one-line pointer to every other doc in this
directory. If you are new, read this, then [`01`](01-overview.md) (what Bismuth is) and
[`02`](02-architecture-and-threads.md) (startup + threads).

> Two other "start here" docs exist and are intentionally kept: [`00-overview.md`](00-overview.md) is
> the **modernization** status table (what is active/inert/wired), and [`01-overview.md`](01-overview.md)
> is the conceptual intro. This file is the **architecture map** — the subsystem topology and where each
> doc lives.

---

## 1. The shape of the node

Bismuth is a single multithreaded Python process — `node.py` — that is full node, miner, wallet
server, and API server at once. Transactions are 8-field tuples (`timestamp, sender, recipient,
amount, signature, public_key, operation, openfield`); the `operation`/`openfield` pair is a generic
data channel that features (tokens, aliases, shielded value, VM contracts) ride on top of without
changing the base tuple. PoW is memory-hard "Heavy3"; signatures default to RSA-4096 via the vendored
`polysign` abstraction. Networks: mainnet (`5658`, `ledger_path` `static/ledger.db` → LMDB ledger
`static/blockstore-ledger.db`), testnet (`2829`), regnet (`3030`, mint-on-demand). See [`01`](01-overview.md), [`02`](02-architecture-and-threads.md).

```
                         ┌──────────────────────── node.py ────────────────────────┐
  peers (TCP 5658) ────► │ ThreadedTCPServer → per-conn handler (socket command set)│
  wallets/explorers ───► │           │                                              │
  (legacy socket API)    │           ├─ digest_block()  ── consensus core ──────────│
                         │           │     ├ mining_heavy3 (PoW verify)             │
  REST clients ────────► │ rest_api  │     ├ difficulty / difficulty_lwma (retarget)│
  (HTTP :5659, opt-in)   │ (aiohttp) │     ├ polysign / SignerFactory (sig verify)  │
                         │           │     └ DbHandler → BlockStore (LMDB ledger)   │
                         │  Mempool ◄┘     (in memory)       token_index (plugin)   │
                         │  ConnectionManager → worker() threads → outbound sync     │
                         └───────────────────────────────────────────────────────────┘
```

The consensus pipeline is `digest.py` (`digest_block`): take `db_lock`, validate every tx
(signature → balance/overspend → double-spend), verify PoW against current difficulty, compute the
reward, commit the block (one LMDB transaction), prune the mempool, advance the tip. See [`03`](03-consensus-blocks-digest.md).

---

## 2. Subsystem map (honest status)

### Storage — one LMDB ledger
**State: complete. The ledger is ONE LMDB environment on every network; there is no other database.**

- **The ledger** (`block_store.py`, `blockstore-<ledger name>`): sub-dbs `blocks` (height → block hash +
  tx rows; post-fork one packed `txrec` per tx), `hashes` (block hash → height), `pk`/`pkr` (pubkey
  dedup), `diff` (per-block difficulty), `sig` (signature[:56] index), `addr` (address history), `bal`
  (per-address credited / rewards / debited / fees in integer units), `txid` (post-fork content txid),
  `pkref` (post-fork pubkey registry) and `meta` (schema, tx count, fixed allocations).
  `commit_block` writes a block and all of that in ONE write transaction; `rollback` reverses a reorg
  in one transaction — a crash always leaves a clean block boundary. Every consensus read (overspend
  balance, duplicate signature, difficulty) is an O(1) or ordered-range LMDB read. `dbhandler.py` is a
  thin facade over it. See [`05`](05-database-and-ledger.md).
- **Allocations, not reward rows.** The concluded Development Reward / Hypernode Payouts totals are
  fixed balance allocations (`ledger_genesis.MAINNET_ALLOCATIONS`, meta `alloc`).
- **Bootstrap + upgrade.** A fresh node takes a tip-verified DB-direct snapshot from peers
  ([`43`](43-p2p-snapshot.md)) or seeds genesis and syncs; an older-schema store is upgraded with
  `scripts/upgrade_ledger.py` (the node refuses to start until it is).
- **The mempool** is in memory; **tokens/aliases** live in the `tokens_aliases` plugin's own LMDB store.
- **`kvstore.py`** ([`36`](36-kvstore-engine-seam.md)): the LMDB layer — `open_store`, a thin txn
  passthrough, the msgpack `Codec` and per-ledger `store_path` naming. Migration record:
  [`26`](26-storage-postfork.md).

### Consensus, digest, difficulty + the #23 self-heal
**State: live core unchanged; difficulty self-heal autoheal-on by default, soaked on regnet.**

- **Digest/validation** (`digest.py`, `digest_tx.py`, `chain_ops.py`): the authoritative validate +
  commit pipeline; reorg/rollback rolls back the ledger and every index in one LMDB transaction, then
  the plugin stores (`chain_ops.rollback`). See [`03`](03-consensus-blocks-digest.md).
- **Difficulty controller** (`difficulty.py`): a PID-style retarget recursive on the *stored* previous
  value, 60 s target. The fork-gated **LWMA** retarget (`difficulty_lwma.py`) replaces its output past
  `node.fork_height` (`difficulty.py:106-112`); inert until a fork activates. See [`04`](04-pow-and-difficulty.md).
- **#23 difficulty-divergence detector + guarded self-heal** ([`35`](35-difficulty-divergence-selfheal.md)):
  fixes a self-reinforcing corrupted stored-difficulty fixed point (a node mining at the wrong difficulty). A
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
**State: shipped, always on, owns its own LMDB store.**

Tokens and aliases are out of node core into `plugins/tokens_aliases` (owns its LMDB store via
`token_index.py`, rolled back on every reorg path); it is the node's only token/alias path. Aliases gained mutable ownership (register/transfer/free). See [`27`](27-plugins.md),
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
| [00-overview.md](00-overview.md) | Modernization status table (active / inert / wired) |
| [01-overview.md](01-overview.md) | What Bismuth is: networks, tx model, high-level architecture, entry points |
| [02-architecture-and-threads.md](02-architecture-and-threads.md) | Node startup sequence, threading model, locks, shared state |
| [03-consensus-blocks-digest.md](03-consensus-blocks-digest.md) | Block digestion pipeline, tx validation, rewards/fees, ledger schema |
| [04-pow-and-difficulty.md](04-pow-and-difficulty.md) | Heavy3 PoW, HMAC-DRBG lookup file, difficulty retarget, LWMA gate |
| [05-database-and-ledger.md](05-database-and-ledger.md) | The LMDB ledger: sub-dbs, atomic commit/rollback, typed rows, allocations, upgrade script |
| [06-networking-protocol.md](06-networking-protocol.md) | Wire protocol, P2P command catalog, worker lifecycle, peers/ban logic |
| [07-mempool.md](07-mempool.md) | In-memory mempool, acceptance rules, `merge()`, size tiers & fees, locking |
| [08-api-and-commands.md](08-api-and-commands.md) | Legacy socket API (`api_*`), core commands, `commands.py` CLI |
| [09-crypto-wallets-keys.md](09-crypto-wallets-keys.md) | Address/key derivation, signing/verification, polysign, fee formula |
| [10-features.md](10-features.md) | Tokens, plugin hooks, regnet (feature layers) |
| [11-configuration.md](11-configuration.md) | `config.txt` reference (every key), mandatory message, peers files |
| [12-tooling-build-tests.md](12-tooling-build-tests.md) | Send/balance scripts, test suite, ledger tooling, build & CI |
| [13-file-reference.md](13-file-reference.md) | Accurate per-file index of the whole tree |
| [14-known-issues-and-improvements.md](14-known-issues-and-improvements.md) | Verified bugs, fragile wiring, refactor/upgrade list |
| [15-rest-api.md](15-rest-api.md) | The modern, parallel REST/JSON API (**on by default**, reads only; writes need `rest_api_write`) |
| [17-roadmap.md](17-roadmap.md) | Modernization roadmap: phases, shipped vs planned, refactor history |
| [18-hardfork-hf2.md](18-hardfork-hf2.md) | The bundled `hf2` fork: signal-activated scheduler, the consensus change set |
| [20-post-quantum.md](20-post-quantum.md) | Post-quantum signatures: ML-DSA signer, the PQ fork path (held in reserve) |
| [21-mining.md](21-mining.md) | The built-in solo miner (`miner.py`) and the dual-algo PoW signalling |
| [22-shielded.md](22-shielded.md) | Shielded value: stealth addresses, ring signatures, RingCT confidential amounts |
| [23-hd-multisig.md](23-hd-multisig.md) | BIP32/BIP39 HD wallets and M-of-N multisig (native signer + VM vault) |
| [25-security-audit.md](25-security-audit.md) | Adversarial security audit: findings, fixes, regression tests |
| [26-storage-postfork.md](26-storage-postfork.md) | Storage migration record: the ledger is LMDB-only (complete) |
| [27-plugins.md](27-plugins.md) | Modern plugin framework; the `tokens_aliases` plugin (tokens/aliases out of core) |
| [29-hf2-serialization-v2.md](29-hf2-serialization-v2.md) | hf2 binary/integer serialization (authoritative spec) |
| [30-genesis-sync-exceptions.md](30-genesis-sync-exceptions.md) | From-genesis sync & historical validation-exception registry |
| [31-accuser.md](31-accuser.md) | The Accuser: detect/prove/propagate miner equivocation (finality substitutes) |
| [33-browser-wallet.md](33-browser-wallet.md) | Browser wallet + injected `window.bismuth` provider (non-custodial) |
| [35-difficulty-divergence-selfheal.md](35-difficulty-divergence-selfheal.md) | #23 difficulty-divergence detector + guarded self-heal |
| [36-kvstore-engine-seam.md](36-kvstore-engine-seam.md) | The LMDB layer (`kvstore.py`: `open_store`, `Codec`, per-ledger `store_path`) |
| [CHANGELOG-2026-06.md](CHANGELOG-2026-06.md) | Commit-anchored engineering changelog for the 2026-06 session (poker, #23 self-heal, KVStore seam) |
| [README.md](README.md) | Doc-set intro + index (note: index currently lists 00–28 only) |

> **Index gap:** `README.md`'s index stops at doc 28, so docs 29–36 and the changelog are not yet
> linked there — this file ([`00-architecture-overview.md`](00-architecture-overview.md)) is the
> complete, current index. The LMDB layer (`kvstore.py`) has its own page at
> [`36`](36-kvstore-engine-seam.md).

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
