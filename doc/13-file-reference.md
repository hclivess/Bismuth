# 13 — File reference

An accurate per-file index of the tree (supersedes the stale `_FILES_DESCRIPTION.md`). *module* =
imported library; *script* = run directly.

## Node core

| File | Kind | Description |
|---|---|---|
| `node.py` | script | the full node (entry point); startup, threading, TCP server, command dispatch |
| `node_init.py` | module | node bootstrap/init helpers lifted from `node.py` by DI: `setup_net_type`, `node_block_init`, `ram_init`, `initial_db_check`, `load_keys`, `add_indices` |
| `node_stop.py` | script | sends `stop` to a local node |
| `digest.py` | module | block & transaction validation/commit pipeline (consensus core) |
| `chain_ops.py` | module | chain-maintenance ops lifted from `node.py` by DI: `rollback`, `recompress_ledger`, `ledger_check_heights`, `blocknf` (block-not-found rollback), plus boot/validation `bootstrap`, `check_integrity`, `sequencing_check` |
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
| `dbhandler.py` | module | `DbHandler` — SQLite connection lifecycle + low-level SQL plumbing + canonical `sql_trace_callback`; composes the two mixins below |
| `dbhandler_queries.py` | module | `DbQueriesMixin` — read-only ledger/index queries (heights, hashes, aliases, balances) |
| `dbhandler_write.py` | module | `DbWriteMixin` — ledger write & rollback ops (block commit, drive flush, dev/hn rewards, index rollbacks) |
| `db_hashes.py` | module | static known-good early-block hash table |
| `ledger_queries.py` | module | `LedgerQueries` classmethod helpers (plugins/hypernodes) |
| `balances.py` | module | `balanceget` — authoritative mempool-aware balance for the `balanceget*` socket commands (lifted from `node.py` by DI) |
| `ledger_explorer.py` | script | early standalone block explorer (unmaintained) |
| `quantizer.py` | module | `Decimal` rounding helpers (2/8/10 dp) |
| `mempool.py` | module | `Mempool` — DB plumbing + consensus admission (`merge`, `space_left_for_tx`); composes the query mixin |
| `mempool_queries.py` | module | `MempoolQueriesMixin` — mempool read/reporting & maintenance (`mp_get`, `status`, `tx_to_send`, `sig_check`, `purge`/`clear`…) |
| `mempool_sql.py` | module | mempool SQL statements + tuning constants (re-exported by `mempool` via `import *`) |

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
| `apihandler.py` | module | `ApiHandler` — the `api_*` dispatcher + small control commands; composes the domain mixins below |
| `apihandler_blocks.py` | module | `BlockApiMixin` — block-oriented read API (`api_getblock*`) |
| `apihandler_address.py` | module | `AddressApiMixin` — address & balance read API (`api_getaddress*`, `api_*balance`, `api_*received`) |
| `apihandler_tx.py` | module | `TxApiMixin` — transaction read API (`api_gettransaction*`) |
| `block_format.py` | module | pure block→JSON formatters (`blockstojson` / `blocktojsondiffs`), extracted from `apihandler` |
| `commands.py` | script | CLI wrapper over node socket commands |
| `check_tx.py` | script | report a txid's status (reads the DBs directly) |
| `attic/demo_getstatus.py`, `attic/demo_getaddresssince.py` | scripts | single-command examples (retired to `attic/`; use the REST API) |
| `cmd_addpeers.py`, `cmd_hn_reg_round.py`, `cmd_hn_last_block_ts.py` | scripts | peer add / hypernode-plugin queries |
| `process_search.py` | module | check whether a process is running |

## Crypto / wallets

| File | Kind | Description |
|---|---|---|
| `essentials.py` | module | helper functions (fees, balances, checkpoints, consensus tallies, tx formatting); the wallet/key cluster now lives in `wallet_helpers` and is re-bound here for back-compat |
| `wallet_helpers.py` | module | wallet & key management lifted from `essentials`: `sign_rsa` + `keys_check`/`keys_save`/`keys_load`/`keys_unlock`/`keys_load_new` |
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
