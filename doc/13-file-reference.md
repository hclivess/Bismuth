# 13 — File reference

An accurate per-file index of the tree (supersedes the stale `_FILES_DESCRIPTION.md`). *module* =
imported library; *script* = run directly.

## Node core

| File | Kind | Description |
|---|---|---|
| `node.py` | script | the full node (entry point); startup, threading, TCP server, command dispatch; `verify` is **fork-aware** (`node.py:1254`) — at/after `fork_height` single-sig rows route through `SignerFactory.verify_tx_signature` (recoverable-over-txid, pubkey dropped) (doc/29) |
| `node_init.py` | module | node bootstrap/init helpers lifted from `node.py` by DI: `setup_net_type`, `open_ledger` (open the LMDB ledger; fresh install → peer snapshot or genesis; refuses an old schema and names `scripts/upgrade_ledger.py`; applies the fixed allocations), `node_block_init`, `load_keys` |
| `node_stop.py` | script | sends `stop` to a local node |
| `digest.py` | module | block validation/commit pipeline (consensus core): the `BlockProcessor` engine + `digest_block`/`process_block_data` orchestration + helpers |
| `digest_tx.py` | module | block/tx data model lifted from `digest`: `Transaction`/`MinerTransaction`/`Block` value objects + the consensus quantizers (`quantize_two`/`quantize_eight`, which differ from `quantizer.py`) |
| `chain_ops.py` | module | chain-maintenance ops lifted from `node.py` by DI: `rollback` (ledger + every index in one LMDB transaction, then the plugin stores), `blocknf` (measured fork resolution, doc/47), `seed_genesis`, `bootstrap` (DB-direct peer snapshot, doc/43), and the doc/35 difficulty-divergence detector + guarded self-heal |
| `fork_resolution.py` | module | measured fork resolution ported from nado (tri-state ancestor probes, possession-before-rollback, one-shot tie-break, corroborated deep reorgs) (doc/47) |
| `validation_exceptions.py` | module | historical validation-exception registry + block-hash checkpoints for from-genesis sync (doc/30) |
| `fork.py` | module | hardfork heights & post-fork reward validation |
| `ledger_genesis.py` | module | the canonical genesis block (height 1, seeded into a fresh ledger) + `MAINNET_ALLOCATIONS`, the fixed Development Reward / Hypernode Payouts balance totals |
| `options.py` | module | config loader (imported as `config` in the node) |
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
| `bismuth_serialize.py` | module | the FROZEN consensus byte forms (signing buffer, block hash) centralised — the boundary storage/API rework must not move; **+ hf2 stage-0** `signature_buffer_v2`/`tx_id_v2` (native-integer + binary pre-image, dormant, caller-gated on `fork_height`) folded into the single hf2 fork (doc/29) |
| `gpuminer/` | dir | vendored Heavy3 GPU miners — CUDA (kbkminer) + OpenCL (`opencl_alt/`); coupled to the PoW (`gpuminer/README.md`) |

## Storage

| File | Kind | Description |
|---|---|---|
| `block_store.py` | module | **the ledger**: ONE LMDB environment (`BlockStore`, `blockstore-<ledger name>`) — sub-dbs `blocks`, `hashes`, `pk`/`pkr` (pubkey dedup), `diff`, `sig`, `addr`, `bal`, `txid`, `pkref`, `meta`; `commit_block` / `rollback` are one transaction each; `typed_row` restores the column types; `reindex` rebuilds every index from the blocks; `stream_snapshot` serves the doc/43 snapshot (doc/05) |
| `dbhandler.py` | module | `DbHandler` — thin thread-safe facade over the shared `BlockStore` with the semantic read helpers the node calls (`last_block_hash`, `pubkeyget`, `blocksync` …), `commit_block` and `rollback_under` |
| `kvstore.py` | module | the LMDB layer: `open_store` / `KVStore` / `Codec` (msgpack, big-endian height keys) and `store_path` (per-ledger store naming) (doc/36) |
| `token_index.py` | module | token+alias derived index on its own per-ledger LMDB store (`TokenIndex`), owned by the always-on `tokens_aliases` plugin (doc/27) |
| `amounts.py` | module | exact integer (atomic-unit) ↔ legacy-string amount conversion — keeps consensus/legacy-API strings byte-identical |
| `txrec.py`, `sigbytes.py`, `addrbytes.py`, `txfields.py` | modules | post-fork true-bytes tx record codecs (one packed `txrec` per tx; raw signature / address / numeric-field encodings) (doc/40) |
| `sync_codec.py` | module | compact binary encoding of the REST sync payload (doc/40) |
| `replay_verify.py` | script | recompute every block hash at the consensus boundary (`bismuth_serialize.block_hash`) vs the stored chain |
| `db_hashes.py` | module | static known-good early-block hash table |
| `balances.py` | module | `balanceget` — authoritative mempool-aware balance for the `balanceget*` socket commands, from the ledger's `bal` entry (lifted from `node.py` by DI) |
| `snapshot_p2p.py` | module | DB-direct ledger snapshot: serve `/api/snapshot[/info]` from the live LMDB env (`env.copyfd`), fetch + tip-verify one from peers (doc/43) |
| `quantizer.py` | module | `Decimal` rounding helpers (2/8/10 dp) |
| `mempool.py` | module | `Mempool` — the in-memory pending-tx pool (signature-keyed, insertion-ordered) + admission (`merge`, `space_left_for_tx`) |

## Modernization

| File | Kind | Description |
|---|---|---|
| `fee_dynamics.py` | module | dynamic/EIP-1559-style fee schedule (post-fork, gated) — see doc/18 |
| `difficulty_lwma.py` | module | fork-gated LWMA retarget (also listed under Consensus/PoW) — pure; live at/after `node.fork_height` (doc/18) |

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
| `tor_manager.py` | module | Tor lifecycle (off / external / managed, ephemeral v3 onion) (doc/38) |
| `rest_api.py` | module | read-only modern REST/JSON API server (status/blocks/balance/tx/peers); `/headers/range` (headers-first) + `/blocks/range?format=sync` (consensus-faithful digester tuples) + `/api/stats/{summary,monthly,tx_per_month,new_addresses,rich_list,top_miners,largest_txs,market,difficulty,geo}` (delegated to `rest_stats`) |
| `rest_stats.py` | module | explorer stats payloads (doc/15): cheap on-demand `network_summary`/`difficulty_series` + background-cached `tx_per_month` (full-ledger histogram) + best-effort `geo_nodes` peer geolocation (gated `rest_api_geo`) |
| `rest_client.py` | module | stdlib REST client: capability discovery + parallel block fetch (`parallel_fetch`/`parallel_fetch_sync`) + `fetch_headers` for headers-first quick sync |
| `api_sync.py` | module | capability-gated, fail-soft headers-first chain-segment fetch (`sync_segment`) — the seam between `rest_client` (transport) and the digester (consensus) |
| `api_sync_worker.py` | module | the opt-in `api_sync` catch-up loop: pulls the chain from a peer's REST API into the digester |
| `rpc_bitcoin.py` | module | bitcoind-compatible JSON-RPC adapter (flag `rpc_bitcoin`, off, :8332): ~32 methods — chain/header/mempool/mining/network reads, getrawtransaction/getbalance/estimatesmartfee, gated sendrawtransaction/submitblock; post-hf2 content-txids over the LMDB ledger; honest -32601 for UTXO/Script/PSBT/wallet |
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
| `plugins.py` | module | `PluginManager` (action/filter hooks) |
| `plugin_base.py` | module | modern plugin framework (doc/27): typed `BismuthPlugin` base (`setup`/`backfill`/`on_block`/`on_rollback` + service/REST/peer-command surfaces) |
| `plugins/tokens_aliases/` | package | the tokens+aliases plugin, always on — the node's only token/alias path: owns the LMDB `TokenIndex`, the `token:issue`/`token:transfer`, `alias=` and `alias:register`/`alias:transfer`/`alias:free` ops, and their socket/REST queries (doc/27) |
| `regnet.py` | module | regression-test network |

## No-GUI / helper scripts

| File | Kind | Description |
|---|---|---|
| `send_nogui_noconf.py` | script | build/sign/submit a transaction without prompts |
| `send_csv.py` | script | batch payouts from `rewards.csv` (wraps `send_nogui_noconf.py`) |
| `balance_nogui.py` | script | print an address balance breakdown |

## Config & data

`config.txt`, `config_custom.txt` (gitignored), `mandatory_message.json`, `peers*.txt`,
`suggested_peers*.txt`, `requirements.txt`, `requirements-node.txt`.

## Tests, tooling, assets

`tests/` (see [12](12-tooling-build-tests.md)), `static/` (runtime data: the LMDB ledger
`blockstore-<ledger>` and per-ledger sidecars, + web assets), `install_node.sh` (full node installer:
deps + systemd unit; `scripts/install-node-service.sh` companion), `scripts/upgrade_ledger.py` (upgrade a
pre-LMDB-only block store to the current ledger schema: difficulty replay anchored on peers'
`/api/stats/difficulty`, then `BlockStore.reindex`; node stopped; doc/05), `scripts/migrate_config.py`
(`config.txt` → `config.toml`), `graphics/`, `auto-install/`, `compile_nuitka.cmd`, `setup.iss`,
`.travis.yml`, `pytest.ini`, `doc/`.
