# Bismuth Node — Developer Documentation

This directory documents the Bismuth full‑node codebase in its entirety: architecture, consensus
rules, the database model, the peer‑to‑peer protocol, the API surface, cryptography, and the
supporting tooling. It is written for developers who want to understand, operate, or modify the node.

> These docs were produced from a full read of the source tree and supersede the older,
> partly‑stale `_FILES_DESCRIPTION.md` / `_MOST_USEFUL_FILES.md` (which still reference files that
> no longer exist, e.g. `classes.py`, `keys.py`, `wallet.py`, `recovery.py`, `rollback.py`).

## Project at a glance

| | |
|---|---|
| **What** | Bismuth — a Proof‑of‑Work cryptocurrency / smart‑transaction platform |
| **Language** | Python 3 (single‑process, multi‑threaded node) |
| **Consensus** | PoW "Heavy3" (memory‑hard, 1 GiB lookup file) + PID‑style difficulty retarget, 60 s target block time |
| **Signatures** | RSA‑4096 (via the `polysign` abstraction; ECDSA/ed25519 supported by the lib) |
| **Addresses** | `sha224(public_key_pem).hexdigest()` → 56‑char hex |
| **Storage** | SQLite — `ledger.db` (full), `hyper.db` (pruned "hyperblocks"), `index.db` (aliases/tokens), `mempool.db` |
| **Networking** | Custom protocol: 10‑byte zero‑padded length header + JSON payload over raw TCP |
| **Networks / ports** | mainnet `5658`, testnet `2829`, regnet `3030` |
| **Genesis address** | `4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed` |
| **Reported version** | app `4.5.0.1`; protocol `mainnet0022` (allow `mainnet0021..0023`) |

## How to read these docs

Start with **01 (overview)** and **02 (architecture & threads)** for the big picture, then dive into
the subsystem you care about. Consensus‑critical material lives in **03 (digest)** and **04 (PoW &
difficulty)** — treat those as authoritative for validation rules.

## Index

| Doc | Topic |
|---|---|
| [01-overview.md](01-overview.md) | What Bismuth is, networks, high‑level architecture, repo layout, entry points |
| [02-architecture-and-threads.md](02-architecture-and-threads.md) | Node startup sequence, threading model, locks, shared state |
| [03-consensus-blocks-digest.md](03-consensus-blocks-digest.md) | Block digestion pipeline, transaction validation, rewards/fees, hardforks, ledger schema |
| [04-pow-and-difficulty.md](04-pow-and-difficulty.md) | Heavy3 PoW, `heavy3a.bin`, HMAC‑DRBG, difficulty retarget, legacy mining |
| [05-database-and-ledger.md](05-database-and-ledger.md) | `DbHandler`, the four databases & schemas, RAM/hyper modes, hyperblock pruning, quantizer |
| [06-networking-protocol.md](06-networking-protocol.md) | Wire protocol, P2P command catalog, worker lifecycle, peers/ban logic, RPC client, hyperlane |
| [07-mempool.md](07-mempool.md) | Mempool schema, acceptance rules, `merge()` flow, size tiers & fees, locking |
| [08-api-and-commands.md](08-api-and-commands.md) | Full `ApiHandler` reference, core socket commands, `commands.py` CLI, demo/cmd scripts |
| [09-crypto-wallets-keys.md](09-crypto-wallets-keys.md) | Address/key derivation, `wallet.der`, signing/verification, fee formula, aliases, logging |
| [10-features.md](10-features.md) | Tokens, staking PoC, plugin system + hooks, regnet |
| [11-configuration.md](11-configuration.md) | `config.txt` reference (every key), `mandatory_message.json`, peers files |
| [12-tooling-build-tests.md](12-tooling-build-tests.md) | Send/balance scripts, the test suite, chain snapshot tooling, build & CI |
| [13-file-reference.md](13-file-reference.md) | Accurate per‑file index of the whole tree |
| [14-known-issues-and-improvements.md](14-known-issues-and-improvements.md) | Verified bugs, fragile wiring, and the refactor/upgrade roadmap |
