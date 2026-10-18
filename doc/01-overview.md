# 01 — Overview

## What Bismuth is

Bismuth is a Proof-of-Work cryptocurrency and "smart-transaction" platform written in Python. A
single program — `node.py` — is the full node: it validates and stores the chain, talks to peers,
serves wallets/explorers/exchanges over a TCP API, and accepts mined blocks. There is no separate
"miner" or "wallet server" binary; the node fills all of those roles.

Distinguishing design choices:

- **Transactions are plain tuples** of 8 fields (sender, recipient, amount, signature, public key,
  operation, openfield, timestamp). The `operation` + `openfield` pair is a generic data channel
  used to build features (tokens, aliases, hypernode registration, messages) *on top of* the chain
  without changing consensus.
- **Transaction id (`txid`).** *Pre-fork* the txid is the first 56 chars of the signature. *Post-fork*
  the canonical txid is a **content hash** — `blake2b-256` over the frozen 6-field buffer
  (`timestamp,address,recipient,amount,operation,openfield`, the same pre-image the signature is
  computed on), rendered as 64 lowercase hex chars (`essentials.format_raw_tx` /
  `block_store.txid_of`, amount via `amounts.ledger_value`, so it is storage-mode agnostic). The ledger
  indexes it in its `txid` sub-db at commit. Lookup is **shape-dispatched**: a 64-char lowercase-hex
  query resolves the content txid through that index, anything else goes through the `sig`
  (signature[:56]) index. Pre-fork rows are byte-identical and keep their `signature[:56]` ids.
- **One LMDB ledger.** The whole chain and every index the node reads (difficulty, signatures, address
  history, balances, txids, pubkey registry) live in one LMDB environment; each block commits — and
  each reorg rolls back — in a single transaction. The mempool is in memory (see
  [05](05-database-and-ledger.md), [07](07-mempool.md)).
- **Memory-hard PoW ("Heavy3")** over a deterministic 1 GiB lookup file, with a PID-style difficulty
  controller targeting a 60-second block time (see [04](04-pow-and-difficulty.md)).
- **RSA-4096 signatures** by default, abstracted through `polysign` (which also offers a menu of
  schemes: ECDSA secp256k1/secp256r1, ED25519, and the post-quantum ML-DSA family — 44/65/87). As of
  this revival, polysign is **vendored in-tree** (see [09](09-crypto-wallets-keys.md) and
  [14](14-known-issues-and-improvements.md)). *Post-fork*, only **ordinary single-sig secp256k1**
  switches to a recoverable signature: it signs the 32-byte content txid, carries a 65-byte
  recoverable hex sig, **drops the `public_key` field** (the signer is recovered via `ecrecover`, with
  low-s enforced). RSA, ED25519, native multisig, and shielded/RingCT keep their existing legacy
  signing post-fork (multisig still signs the frozen buffer with explicit pubkeys, not the txid).
- **A bespoke wire protocol**: a 10-byte zero-padded length header followed by a JSON payload, over
  a raw TCP socket (see [06](06-networking-protocol.md)).

## Networks

| Network | Purpose | Port | `ledger_path` → LMDB ledger | Notes |
|---|---|---|---|---|
| **mainnet** | production | 5658 | `static/ledger.db` → `static/blockstore-ledger.db` | protocol `mainnet0023` |
| **testnet** | public test chain | 2829 | `static/ledger_test.db` → `static/blockstore-ledger_test.db` | |
| **regnet** | local regression testing | 3030 | `static/regmode.db` → `static/blockstore-regmode.db` | fixed difficulty, blocks minted on demand via `regtest_generate` |

`ledger_path` names the ledger; the LMDB environment itself is `blockstore-<ledger name>` beside it
(`kvstore.store_path`), so every network has its own store.

The genesis/foundation address is `4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed`.

## High-level architecture

```
                         ┌─────────────────────────── node.py ───────────────────────────┐
   peers (TCP 5658) ───► │  ThreadedTCPServer ──► per-connection handler (command dispatch)│
                         │        │                         │                              │
   wallets / explorers ─►│        │                         ├─ digest_block()  (digest.py) │──► consensus:
   exchanges (API)       │        │                         │     ├ mining_heavy3 (PoW)     │    validate, PoW,
                         │        │                         │     ├ difficulty (retarget)   │    rewards, write
                         │        │                         │     ├ polysign (signatures)   │
                         │        │                         │     └ DbHandler (LMDB ledger) │
                         │        ▼                         ▼                              │
                         │  ConnectionManager ──► Peers.client_loop ──► worker() threads ──┼──► outbound sync
                         │                                  │                              │    to peers
                         │  Mempool (mp.MEMPOOL)  ◄─────────┘                              │
                         └────────────────────────────────────────────────────────────────┘
```

Data flow for a new block:
1. A miner (or peer) sends a `block` (or `blocksfnd` during sync).
2. The handler calls `digest_block()` which takes `node.db_lock`, validates every transaction
   (signatures, balances, double-spends), verifies the PoW against the current difficulty, computes
   the reward, commits the block to the LMDB ledger in one transaction, removes confirmed txs from the mempool, and updates the
   in-memory chain tip.
3. Outbound `worker()` threads relay the new tip to peers; consensus is the most-common block height
   among connected peers.

## Repository layout (top level)

| Path | What |
|---|---|
| `node.py` | the node (entry point) |
| `digest.py` | block & transaction validation / commit pipeline |
| `mining_heavy3.py`, `mining.py`, `difficulty.py`, `hmac_drbg.py` | PoW + difficulty |
| `block_store.py`, `dbhandler.py`, `kvstore.py`, `db_hashes.py`, `quantizer.py`, `ledger_genesis.py` | storage layer (LMDB ledger, its facade, genesis + allocations) |
| `mempool.py` | unconfirmed-transaction pool (in memory) |
| `connections.py`, `connectionmanager.py`, `peershandler.py`, `worker.py`, `rpcconnections.py` | networking |
| `apihandler.py`, `commands.py` | API surface + CLI |
| `essentials.py`, `simplecrypt.py`, `wallet_keys.py`, `polysign/` | crypto / wallets / signatures |
| `plugins/tokens_aliases/`, `token_index.py`, `plugins.py`, `plugin_base.py`, `regnet.py` | feature layers |
| `options.py`, `config.toml`, `mandatory_message.json`, `peers*.txt` | configuration / data |
| `fork.py`, `ledger_genesis.py`, `chain_ops.py`, `snapshot_p2p.py` | hardfork rules / chain bootstrap |
| `libs/` | small data-holder classes (`Node`, `Logger`, `Keys`, `Client`) |
| `tests/` | pytest suite + the dependency-light `regnet_smoke.py` |
| `static/`, `graphics/`, `auto-install/` | tooling, assets, installer |
| `doc/` | this documentation |

## Entry points

| Command | Purpose |
|---|---|
| `python3 node.py` | run a node (reads `config.toml` / `config_custom.toml`) |
| `python3 node.py regnet2` | run a regnet node (used by the tests) |
| `python3 node_stop.py` | ask a local node to stop (sends the `stop` command) |
| `python3 commands.py <cmd> [args]` | CLI wrapper over node socket commands |
| `python3 send_nogui_noconf.py …` | build/sign/submit a transaction without prompts |
| `python3 tests/regnet_smoke.py` | run the dependency-light smoke/characterization gate |

The GUI/CLI **wallet** lives in separate repositories (TornadoWallet, tk-wallet); this repo is the
node and its no-GUI helpers.

See [02](02-architecture-and-threads.md) for the startup sequence and threading model.
