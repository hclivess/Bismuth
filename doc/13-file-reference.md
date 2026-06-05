# 13 — File reference

An accurate per-file index of the tree (supersedes the stale `_FILES_DESCRIPTION.md`). *module* =
imported library; *script* = run directly.

## Node core

| File | Kind | Description |
|---|---|---|
| `node.py` | script | the full node (entry point); startup, threading, TCP server, command dispatch |
| `node_stop.py` | script | sends `stop` to a local node |
| `digest.py` | module | block & transaction validation/commit pipeline (consensus core) |
| `fork.py` | module | hardfork heights & post-fork reward validation |
| `genesis.py` | script | one-shot chain bootstrap (creates `ledger.db`); legacy wallet format |
| `options.py` | module | config loader (imported as `config` in the node) |
| `application_directories.py` | module | tiny path helper (not used by the node) |
| `libs/node.py`, `libs/logger.py`, `libs/keys.py`, `libs/client.py` | modules | data-holder classes (`Node`, `Logger`, `Keys`, `Client`) |

## Consensus / PoW

| File | Kind | Description |
|---|---|---|
| `mining_heavy3.py` | module | Heavy3 PoW + `check_block`; manages `heavy3a.bin` |
| `mining.py` | module | legacy PoW (unused; kept for reference) |
| `difficulty.py` | module | difficulty retarget |
| `hmac_drbg.py` | module | HMAC-DRBG (SHA512) that seeds `heavy3a.bin` |

## Storage

| File | Kind | Description |
|---|---|---|
| `dbhandler.py` | module | `DbHandler` — all SQLite connections & queries |
| `db_hashes.py` | module | static known-good early-block hash table |
| `ledger_queries.py` | module | `LedgerQueries` classmethod helpers (plugins/hypernodes) |
| `ledger_explorer.py` | script | early standalone block explorer (unmaintained) |
| `quantizer.py` | module | `Decimal` rounding helpers (2/8/10 dp) |
| `mempool.py` | module | the `Mempool` class |

## Networking

| File | Kind | Description |
|---|---|---|
| `connections.py` | module | low-level wire protocol (10-byte length + JSON) |
| `connectionmanager.py` | module | the `ConnectionManager` thread |
| `peershandler.py` | module | the `Peers` class (discovery, bans, consensus, persistence) |
| `worker.py` | module | outbound per-peer sync thread |
| `rpcconnections.py` | module | client-side `Connection` class (for wallets/scripts) |
| `hyperlane.py` | module | placeholder hyperlane manager (stub); `attic/hyperlane_asyncio.py` is the retired asyncio variant |

## API / CLI

| File | Kind | Description |
|---|---|---|
| `apihandler.py` | module | `ApiHandler` — the `api_*` command surface |
| `commands.py` | script | CLI wrapper over node socket commands |
| `check_tx.py` | script | report a txid's status (reads the DBs directly) |
| `attic/demo_getstatus.py`, `attic/demo_getaddresssince.py` | scripts | single-command examples (retired to `attic/`; use the REST API) |
| `cmd_addpeers.py`, `cmd_hn_reg_round.py`, `cmd_hn_last_block_ts.py` | scripts | peer add / hypernode-plugin queries |
| `process_search.py` | module | check whether a process is running |

## Crypto / wallets

| File | Kind | Description |
|---|---|---|
| `essentials.py` | module | crypto & helper functions (sign/verify, fees, balances, key load/save, checkpoints) |
| `polysign/` | package | **vendored** signatures library (RSA + lazy ECDSA/ED25519/BTC/CRW) |
| `simplecrypt.py` | module | AES-256-CTR wallet encryption (vendored `simple-crypt`) |
| `wallet_keys.py` | module | minimal `wallet.der` reader / keypair generator |

## Features

| File | Kind | Description |
|---|---|---|
| `tokensv2.py` | module | token issue/transfer indexing |
| `aliases.py` / `aliasesv2.py` | modules | alias indexing (v1 `alias=` / v2 `alias:register`) |
| `staking.py` | module | offline-staking proof of concept (experimental) |
| `plugins.py` | module | `PluginManager` (action/filter hooks) |
| `regnet.py` | module | regression-test network |

## No-GUI / helper scripts

| File | Kind | Description |
|---|---|---|
| `send_nogui_noconf.py` | script | build/sign/submit a transaction without prompts |
| `send_csv.py` | script | batch payouts from `rewards.csv` (wraps `send_nogui_noconf.py`) |
| `balance_nogui.py` | script | print an address balance breakdown |
| `attic/rewards_reindex.py`, `attic/rewards_test.py` | scripts | one-off dev-reward backfill / check (retired to `attic/`) |

## Config & data

`config.txt`, `config_custom.txt` (gitignored), `mandatory_message.json`, `peers*.txt`,
`suggested_peers*.txt`, `requirements.txt`, `requirements-node.txt`.

## Tests, tooling, assets

`tests/` (see [12](12-tooling-build-tests.md)), `static/` (snapshot/maintenance tooling + web assets),
`graphics/`, `auto-install/`, `compile_nuitka.cmd`, `setup.iss`, `.travis.yml`, `pytest.ini`, `doc/`.
