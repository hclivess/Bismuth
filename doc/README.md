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
| **Storage** | SQLite — `ledger.db` (full), `hyper.db` (pruned "hyperblocks"), `index.db` (aliases/tokens), `mempool.db`; post‑fork migrating to a single LMDB store (block store, shielded, token/alias index — [26](26-storage-postfork.md)) |
| **Networking** | Custom protocol: 10‑byte zero‑padded length header + JSON payload over raw TCP |
| **Networks / ports** | mainnet `5658`, testnet `2829`, regnet `3030` |
| **Genesis address** | `4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed` |
| **Reported version** | app `4.5.0.1`; protocol `mainnet0023` (allow `mainnet0021..0023`) |

## How to read these docs

Start with **00 (modernization overview)** for where the project is today, then **01 (overview)** and
**02 (architecture & threads)** for the big picture, then dive into the subsystem you care about. Consensus‑critical material lives in **03 (digest)** and **04 (PoW &
difficulty)** — treat those as authoritative for validation rules.

## Index

| Doc | Topic |
|---|---|
| [00-overview.md](00-overview.md) | **Modernization overview (start here)**: what exists, what's active, what's inert, what's planned |
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
| [15-rest-api.md](15-rest-api.md) | The modern, parallel, opt-in REST/JSON API |
| [16-database-rework-plan.md](16-database-rework-plan.md) | Design/roadmap for a complete storage-layer modernization |
| [17-roadmap.md](17-roadmap.md) | Modernization roadmap: phases, what's shipped, refactor history |
| [18-hardfork-hf2.md](18-hardfork-hf2.md) | The bundled `hf2` hard fork: signal-activated scheduler, serialization/difficulty/PoW changes |
| [19-vm.md](19-vm.md) | The decentralized-apps VM: RISC-V (RV32I) engine, state root, value custody, contract flexibility (CALL/DELEGATECALL/SETCODE/SELFDESTRUCT), HTLC |
| [44-contracts.md](44-contracts.md) | **Writing Bismuth smart contracts** — complete developer manual: execution model, full syscall ABI, storage, custody, composition & upgradeability, security, testing, demo catalog |
| [20-post-quantum.md](20-post-quantum.md) | Post-quantum signatures: ML-DSA-65 signer, the `pq` fork path |
| [21-mining.md](21-mining.md) | The built-in solo miner (`miner.py`) and dual-algo PoW signalling |
| [22-shielded.md](22-shielded.md) | Shielded value: stealth addresses, ring signatures, RingCT confidential amounts (`shieldedv1.py`, `ringct.py`, `bulletproof.py`) |
| [23-hd-multisig.md](23-hd-multisig.md) | BIP32/BIP39 HD wallets and M‑of‑N multisig (native signer + VM vault) |
| [24-defi-dex.md](24-defi-dex.md) | DeFi on the VM: DEX order book, constant-product AMM, multi-pool any-token router; HTLC/atomic-swap plans |
| [25-security-audit.md](25-security-audit.md) | Adversarial security audit: findings, fixes, regression tests |
| [26-storage-postfork.md](26-storage-postfork.md) | Post-fork storage rearchitecture: retiring SQLite for one LMDB store, staged migration |
| [27-plugins.md](27-plugins.md) | Modern plugin framework; the `tokens_aliases` plugin (tokens/aliases out of core) |
| [28-poker.md](28-poker.md) | On-chain heads-up Texas Hold'em: escrow + commit-reveal + on-chain hand evaluation, off-chain mental-poker deal |
