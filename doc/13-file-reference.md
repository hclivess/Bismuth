# 13 — File reference

An accurate per-file index of the tree (supersedes the stale `_FILES_DESCRIPTION.md`). *module* =
imported library; *script* = run directly.

## Node core

| File | Kind | Description |
|---|---|---|
| `node.py` | script | the full node (entry point); startup, threading, TCP server, command dispatch; `verify` is **fork-aware** (`node.py:1254`) — at/after `fork_height` single-sig rows route through `SignerFactory.verify_tx_signature` (recoverable-over-txid, pubkey dropped) (doc/29) |
| `node_init.py` | module | node bootstrap/init helpers lifted from `node.py` by DI: `setup_net_type`, `node_block_init`, `ram_init`, `initial_db_check`, `load_keys`, `add_indices` |
| `node_stop.py` | script | sends `stop` to a local node |
| `digest.py` | module | block validation/commit pipeline (consensus core): the `BlockProcessor` engine + `digest_block`/`process_block_data` orchestration + helpers |
| `digest_tx.py` | module | block/tx data model lifted from `digest`: `Transaction`/`MinerTransaction`/`Block` value objects + the consensus quantizers (`quantize_two`/`quantize_eight`, which differ from `quantizer.py`) |
| `chain_ops.py` | module | chain-maintenance ops lifted from `node.py` by DI: `rollback`, `recompress_ledger`, `ledger_check_heights`, `blocknf` (block-not-found rollback), plus boot/validation `bootstrap`, `check_integrity`, `sequencing_check` |
| `fork.py` | module | hardfork heights & post-fork reward validation |
| `genesis.py` | script | one-shot chain bootstrap (creates `ledger.db`); legacy wallet format |
| `options.py` | module | config loader (imported as `config` in the node) |
| `application_directories.py` | module | tiny path helper (not used by the node) |
| `log.py` | module | logger setup: rotating file handler + optional ANSI-coloured console output (`log_color`) |
| `libs/node.py`, `libs/logger.py`, `libs/keys.py`, `libs/client.py` | modules | data-holder classes (`Node`, `Logger`, `Keys`, `Client`) |

## Consensus / PoW

| File | Kind | Description |
|---|---|---|
| `mining_heavy3.py` | module | Heavy3 PoW + `check_block`; manages `heavy3a.bin`; **dual-algo** (`new_pow`: sha224→blake2b) at the single hf2 fork height |
| `miner.py` | module | built-in solo miner — builds + mines (Heavy3) + digests on the tip; stamps the `hf2` coinbase signal; opt-in `mine=True` (doc/21) |
| `mining.py` | module | legacy PoW (unused; kept for reference) |
| `difficulty.py` | module | difficulty retarget (legacy PID controller) |
| `difficulty_lwma.py` | module | hf2 LWMA retarget — symmetric/delicate/calculable; pure + **live fork-gated**: at/after `node.fork_height` `difficulty.py` (`difficulty.py:101`) routes its output through `lwma_next_difficulty` (doc/18) |
| `fork.py` | module | hardfork heights + post-fork reward validation; **+ the deterministic signal-activated `dynamic_fork_height` scheduler** (doc/18) — **live**: `digest.py` (`digest.py:448`) runs it each digest and persists the locked height (replayed at startup) |
| `hmac_drbg.py` | module | HMAC-DRBG (SHA512) that seeds `heavy3a.bin` |
| `bismuth_serialize.py` | module | the FROZEN consensus byte forms (signing buffer, block hash) centralised — the boundary storage/API rework must not move (doc/16); **+ hf2 stage-0** `signature_buffer_v2`/`tx_id_v2` (native-integer + binary pre-image, dormant, caller-gated on `fork_height`) folded into the single hf2 fork (doc/29) |
| `gpuminer/` | dir | vendored Heavy3 GPU miners — CUDA (kbkminer) + OpenCL (`opencl_alt/`); coupled to the PoW (`gpuminer/README.md`) |

## Storage

| File | Kind | Description |
|---|---|---|
| `dbhandler.py` | module | `DbHandler` — SQLite connection lifecycle + low-level SQL plumbing + canonical `sql_trace_callback`; composes the two mixins below |
| `dbhandler_queries.py` | module | `DbQueriesMixin` — read-only ledger/index queries (heights, hashes, aliases, balances) |
| `dbhandler_write.py` | module | `DbWriteMixin` — ledger write & rollback ops (block commit, drive flush, dev/hn rewards, index rollbacks) |
| `block_store.py` | module | LMDB append-only block-body store (`BlockStore`) — phase-7 scalable storage foundation; lossless mirror of the ledger (`build_from_sqlite`/`verify_against_sqlite`), height-keyed with a hash→height index and reorg `rollback` |
| `storage_backend.py` | module | the storage READ/WRITE seam (doc/26 stages 3–4): one block/tx interface, `SqliteBackend` + `LmdbBackend` (+ `LmdbWriteBackend`) with `cross_check` — the strangler-fig route off SQLite |
| `token_index.py` | module | token+alias derived index on **LMDB** (`TokenIndex`) — post-fork successor to the SQLite `index.db` projection (doc/26 stage 2); owned by the `tokens_aliases` plugin (doc/27), flag `token_index` |
| `balance_index.py` | module | maintained O(1) per-address balance index (`BalanceIndex`) in integer units — bit-matches `ledger_balance3`; phase-7 replacement for the full-scan balance |
| `balance_cache.py` | module | per-`(address, height)` memo over the authoritative balance — O(1) repeat reads between blocks, invalidated on a new block |
| `amounts.py` | module | exact integer (atomic-unit) ↔ legacy-string amount conversion (doc/16 phase 2) — keeps consensus/legacy-API strings byte-identical |
| `migrate_amounts.py` | script | one-off TEXT→INTEGER ledger amount migration + replay verification (doc/16 phase 2) |
| `db_helpers.py` | module | `retry_db` — the shared retry-until-it-works DB loop (replaces the per-call-site copies) |
| `db_migrations.py` | module | versioned, idempotent SQLite schema migrations (`user_version`-tracked steps) |
| `replay_verify.py` | script | recompute every block hash at the consensus boundary (`bismuth_serialize.block_hash`) vs the stored chain — storage-rework safety net |
| `_lmdb_demo.py` | script | real-scale proof: build the LMDB `BlockStore` from the live mainnet ledger, verify lossless + consensus-faithful, measure size |
| `reward_chain.py` | module | reward sidechain (`RewardChain`) — lifts the locally-minted dev/hypernode reward 'mirror' rows out of the main ledger into a height-keyed store (phase-5; balance-preserving) |
| `db_hashes.py` | module | static known-good early-block hash table |
| `ledger_queries.py` | module | `LedgerQueries` classmethod helpers (plugins/hypernodes) |
| `balances.py` | module | `balanceget` — authoritative mempool-aware balance for the `balanceget*` socket commands (lifted from `node.py` by DI) |
| `ledger_explorer.py` | script | early standalone block explorer (unmaintained) |
| `quantizer.py` | module | `Decimal` rounding helpers (2/8/10 dp) |
| `mempool.py` | module | `Mempool` — DB plumbing + consensus admission (`merge`, `space_left_for_tx`); composes the query mixin |
| `mempool_queries.py` | module | `MempoolQueriesMixin` — mempool read/reporting & maintenance (`mp_get`, `status`, `tx_to_send`, `sig_check`, `purge`/`clear`…) |
| `mempool_sql.py` | module | mempool SQL statements + tuning constants (re-exported by `mempool` via `import *`) |

## Modernization / VM

| File | Kind | Description |
|---|---|---|
| `bismuth_riscv.py` | module | the RV32I RISC-V interpreter — the single deterministic contract execution engine (doc/19) |
| `vm_engine.py` | module | contract deploy/call orchestration over `bismuth_riscv` — gas/value custody, HTLC, host calls (doc/19) |
| `vm_state.py` | module | contract state store + the ENFORCED state root committed into the coinbase (doc/19) |
| `fee_dynamics.py` | module | dynamic/EIP-1559-style fee schedule (post-fork, gated) — see doc/18 |
| `difficulty_lwma.py` | module | fork-gated LWMA retarget (also listed under Consensus/PoW) — pure; live at/after `node.fork_height` (doc/18) |
| `contracts/` | dir | demo/reference VM contracts (`dex.py`, `amm.py`, `router.py`, `poker.py`, `multisig.py`, `escrow.py`, `vesting.py`, `prediction_market.py`, `raffle.py`, `token_contract.py`) + `asmtools.py` assembler helpers (doc/19, doc/24, doc/28); `asmtools.assemble()` relaxes out-of-range conditional branches (jal) for large contracts |
| `web/amm/`, `web/dex/`, `web/router/`, `web/poker/` | dir | demo SPAs + localhost signing relays for the AMM, DEX, multi-pool router (doc/24) and heads-up poker (doc/28; the poker relay also bridges the off-chain mental-poker deal) |

## Shielded value (doc/22)

| File | Kind | Description |
|---|---|---|
| `shieldedv1.py` | module | stages 1+2: stealth addresses + linkable ring signatures (key images), `shield:mint/spend/redeem` consensus validation/apply + the LMDB `ShieldedState`; dispatches v1/v2/v3 |
| `ringct.py` | module | stage 3: RingCT confidential amounts — Pedersen commitments + 2-column MLSAG; the v3 note/spend formats (doc/22 §13) |
| `bulletproof.py` | module | aggregatable Bulletproof range proofs (batch-verifiable) backing RingCT outputs |

## Networking

| File | Kind | Description |
|---|---|---|
| `connections.py` | module | low-level wire protocol (10-byte length + JSON) |
| `connectionmanager.py` | module | the `ConnectionManager` thread |
| `peershandler.py` | module | the `Peers` manager — `__slots__` + `__init__` + net-type helpers + the `client_loop` maintenance orchestrator; composes the four domain mixins below |
| `peers_storage.py` | module | `PeersStorageMixin` — peer-file disk I/O (`peers_get`/`peers_test`/`peer_list_disk_format`) + inbound `peersync` |
| `peers_pool.py` | module | `PeersPoolMixin` — outbound connection-pool membership + retry/back-off (`can_connect_to`, `add_try`/`del_try`/`reset_tried`) |
| `peers_consensus.py` | module | `PeersConsensusMixin` — consensus-height tracking (`consensus_add`/`consensus_remove` + the `consensus_*` vote properties) |
| `peers_access.py` | module | `PeersAccessMixin` — bans, weighted `warning`s, whitelist checks, mainnet-version gating |
| `peers_reputation.py` | module | per-peer reputation (proven valid-PoW deliveries) — drives reward/penalize, reputation-weighted tip, and the rollback-consensus gate |
| `worker.py` | module | outbound per-peer sync thread |
| `rpcconnections.py` | module | client-side `Connection` class (for wallets/scripts) |
| `hyperlane.py` | module | placeholder hyperlane manager (stub); `attic/hyperlane_asyncio.py` is the retired asyncio variant |
| `rest_api.py` | module | read-only modern REST/JSON API server (status/blocks/balance/tx/peers); `/headers/range` (headers-first) + `/blocks/range?format=sync` (consensus-faithful digester tuples) + `/api/stats/{summary,monthly,tx_per_month,new_addresses,rich_list,top_miners,largest_txs,market,difficulty,geo}` (delegated to `rest_stats`) |
| `rest_stats.py` | module | explorer stats payloads (doc/15): cheap on-demand `network_summary`/`difficulty_series` + background-cached `tx_per_month` (full-ledger histogram) + best-effort `geo_nodes` peer geolocation (gated `rest_api_geo`) |
| `rest_client.py` | module | stdlib REST client: capability discovery + parallel block fetch (`parallel_fetch`/`parallel_fetch_sync`) + `fetch_headers` for headers-first quick sync |
| `api_sync.py` | module | capability-gated, fail-soft headers-first chain-segment fetch (`sync_segment`) — the seam between `rest_client` (transport) and the digester (consensus) |
| `rpc_bitcoin.py` | module | bitcoind-compatible JSON-RPC adapter (flag `rpc_bitcoin`, off, :8332): ~32 methods — chain/header/mempool/mining/network reads, getrawtransaction/getbalance/estimatesmartfee, gated sendrawtransaction/submitblock; post-hf2 content-txids over block_store; honest -32601 for UTXO/Script/PSBT/wallet |
| `rpc_ethereum.py` | module | `eth_*` compatibility adapter (flag `rpc_ethereum`, off, :8545): 42 methods incl. eth_call/getCode/getStorageAt/estimateGas over the RISC-V `vm_state`; bounded by *literal* EVM compat (not MetaMask), ROADMAP/DIVERGENCE -32601 for unbacked |
| `transport.py` | module | HTTP transport codecs (gzip/br) negotiated via `Accept-Encoding`; zero hard native deps |

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
| `legacy_sync_probe.py` | script | read-only diagnostic: verifies live wire/protocol compatibility with legacy mainnet peers (handshake / version / peer-list / block-height negotiation) |
| `attic/demo_getstatus.py`, `attic/demo_getaddresssince.py` | scripts | single-command examples (retired to `attic/`; use the REST API) |
| `cmd_addpeers.py`, `cmd_hn_reg_round.py`, `cmd_hn_last_block_ts.py` | scripts | peer add / hypernode-plugin queries |
| `process_search.py` | module | check whether a process is running |

## Crypto / wallets

| File | Kind | Description |
|---|---|---|
| `essentials.py` | module | helper functions (fees, balances, checkpoints, consensus tallies, tx formatting); the wallet/key cluster now lives in `wallet_helpers` and is re-bound here for back-compat |
| `wallet_helpers.py` | module | wallet & key management lifted from `essentials`: `sign_rsa` + `keys_check`/`keys_save`/`keys_load`/`keys_unlock`/`keys_load_new` |
| `polysign/` | package | **vendored** signatures library (RSA + lazy ECDSA/ED25519/BTC/CRW) + ML-DSA-65 post-quantum signer `polysign/signer_mldsa.py` (doc/20) |
| `simplecrypt.py` | module | AES-256-CTR wallet encryption (vendored `simple-crypt`) |
| `wallet_keys.py` | module | minimal `wallet.der` reader / keypair generator |
| `hd_wallet.py` | module | BIP32-style HD wallet (secp256k1 ECDSA): derivation (`m/44'/…`), gap-limit recovery scan — wallet-side only, no consensus change (doc/23) |
| `bip39.py` | module | BIP39 mnemonic codec (standard wordlist `bip39_english.txt`; phrases portable to/from any BIP39 tool) (doc/23) |
| `multisig_wallet.py` | module | build + co-sign native M-of-N multisig spends (polysign `SignerMultisig`) into a complete tx tuple ready for `mpinsert` (doc/23) |

## Features

| File | Kind | Description |
|---|---|---|
| `tokensv2.py` | module | token issue/transfer indexing |
| `aliases.py` / `aliasesv2.py` | modules | alias indexing (v1 `alias=` / v2 `alias:register`); the `tokens_aliases` plugin (doc/27) implements mutable-ownership evolution ops: `alias:register` (claim), `alias:transfer` (owner-only, recipient in openfield), `alias:free` (owner-only release) |
| `staking.py` | module | offline-staking proof of concept (experimental) |
| `plugins.py` | module | `PluginManager` (action/filter hooks) |
| `plugin_base.py` | module | modern plugin framework (doc/27): typed `BismuthPlugin` base (`setup`/`backfill`/`on_block`/`on_rollback` + service/REST/peer-command surfaces) |
| `plugins/tokens_aliases/` | package | the tokens+aliases MODERN plugin — owns the LMDB `TokenIndex`; core defers to it when `token_index` is on (doc/27) |
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
`install_node.sh` (full node installer: deps + systemd unit; `scripts/install-node-service.sh` +
`scripts/snapshot.py` companions), `_mkbootstrap.sh` (ledger-snapshot/bootstrap builder),
`graphics/`, `auto-install/`, `compile_nuitka.cmd`, `setup.iss`, `.travis.yml`, `pytest.ini`, `doc/`.
