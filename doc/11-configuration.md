# 11 — Configuration reference

`options.Get().read()` loads `config.txt`, overrides with `config_custom.txt` if present, then reads
`mandatory_message.json`. Unknown keys are ignored; every key is copied onto `node.*` at startup.
Values are typed per the loader (`int`, `bool` — false for `false/0/""/no` —, `list` comma-split,
`dict` JSON, else `str`).

## Keys

| Key | Type | Meaning |
|---|---|---|
| `port` | int | TCP listen port (mainnet 5658) |
| `verify` | bool | full signature re-verification of the ledger at startup |
| `testnet` / `regnet` | bool | network selection |
| `heavy` | bool | require the Heavy3 `heavy3a.bin` file (false on regnet) |
| `version` | str | protocol version (forced to `mainnet0023` on mainnet at startup, regardless of `config.txt`) |
| `version_allow` | list | accepted peer protocol versions |
| `thread_limit` | int | thread budget (inbound throttled at ~2/3; workers below 3×) |
| `rebuild_db` | bool | **no-op** (the code path is commented out) |
| `debug` | bool | re-raise exceptions in handlers |
| `debug_level` | str | log level (`WARNING`, `DEBUG`, …) |
| `purge` | bool | mempool purge flag |
| `pause` | int | sleep interval used throughout the sync loops |
| `ledger_path` / `hyper_path` | str | ledger.db / hyper.db paths |
| `hyper_recompress` | bool | recompress hyperblocks at startup when heights agree |
| `full_ledger` | bool | keep the full ledger (false → hyperblock-only) |
| `ram` | bool | load the hyperblock ledger into RAM |
| `ban_threshold` | int | warning points before a peer is banned |
| `nodes_ban_reset` | int | pool size under which the banlist auto-resets |
| `tor` | bool | route outbound via SOCKS5; suppress the local server |
| `allowed` | str/list | IPs (or `any`) permitted to issue privileged commands |
| `banlist` / `whitelist` | list | initial banned / never-banned IPs |
| `node_ip` | str | the node's own external IP |
| `light_ip` | dict | `{ip: port}` light/wallet servers |
| `reveal_address` | bool | expose the wallet address in status replies |
| `accept_peers` | bool | accept peer announcements |
| `mempool_allowed` | list | addresses that bypass the 0.5–0.6 MB mempool tier |
| `mempool_ram` | bool | mempool in RAM vs on disk (default true) |
| `mempool_path` | str | on-disk mempool path |
| `terminal_output` | bool | echo logs to stdout |
| `egress` | bool | relay blocks to peers (false = receive-only) |
| `trace_db_calls` | bool | log every SQL statement |
| `heavy3_path` | str | path to `heavy3a.bin` |
| `old_sqlite` | bool | disable the `substr` signature-prefix index optimisation |
| `gui_scaling` | str | wallet-GUI hint (unused by the node) |

After load, `genesis` is hardcoded to `4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed`.

## Modernization keys (storage / consensus / VM — doc/16–19)

Declared in `options.py`, assigned onto `node.*` at startup. All default to a safe value and, where
consensus-affecting, are **inert until the hf2 fork activates**.

| Key | Type | Meaning |
|---|---|---|
| `rollback_consensus` | bool | auto-recovery rollback (default **ON**) — replaces the rigid `rollback_depth`; a stuck tip is recovered by a reputation-gated deep rollback (`essentials.rollback_allowed`) |
| `rollback_consensus_threshold` | int | how far below consensus the local tip must lag before auto-recovery triggers |
| `rollback_consensus_min_peers` | int | minimum peers in consensus before an auto-recovery rollback is allowed |
| `rollback_consensus_min_reputable` | int | minimum **proven-reputation** peers required — anti-sybil gate so a fresh flood can't force a deep reorg |
| `rollback_depth` | int | legacy fixed rollback anchor (superseded by `rollback_consensus`) |
| `block_store` | bool | maintain the LMDB block-store shadow (doc/17 phase 7; write path live, reads still SQLite) |
| `balance_index` | bool | maintain the O(1) balance index — DISPLAY read path (`/api/balance`); consensus still uses `ledger_balance3` |
| `ledger_integer_amounts` | bool | store amounts as integer atomic units (doc/16); **not yet hyperblock-rollup safe — keep off on mainnet** |
| `vm` | bool | enable the decentralized-apps RISC-V VM (doc/19); inert until hf2 |
| `fork_signal` | bool | emit the `hf2` coinbase signal when generating blocks (upgraded miners) |
| `fork_window` / `fork_boundary` / `fork_bury` | int | hf2 activation parameters (signal window, round boundary, burial margin) — `/api/fork` reports status |
| `mine` | bool | run the built-in solo miner (`miner.py`): real Heavy3, embeds mempool txs, writes the hf2 coinbase (doc/21) |
| `pow_fork_signal` | bool | emit the `pow2` signal to activate the modernised blake2b Heavy3 (doc/18-D) |
| `bootstrap_url` / `bootstrap_file` | str | ledger-snapshot source for fast bootstrap (`chain_ops.bootstrap`) |
| `rest_api` / `rest_api_port` | bool/int | enable the read-only REST API (doc/15) and its port |
| `rpc_bitcoin` / `rpc_bitcoin_port`, `rpc_ethereum` / `rpc_ethereum_port` | bool/int | external RPC-bridge config keys (atomic-swap tooling) |

Two modernized subsystems have **no config knob by design**: the **dynamic base fee** (`fee_dynamics.py`)
is computed algorithmically from recent block fullness post-fork, and the **peer-reputation** system
(penalize/reward + the reputation-weighted sync tip) is always-on, tuned in `peers_reputation.py`.

## `mandatory_message.json`

A map of exchange deposit address → human-readable note. Transactions sent to these addresses with a
trivial (`len ≤ 4`) openfield are rejected by the mempool, to stop users losing exchange deposits
that lack the required memo. Override the built-in defaults by editing this file.

## Peer files

Single-line JSON `{ip: port}`: `peers.txt` (mainnet), `peers_test.txt` (testnet), `peers_reg.txt`
(regnet, `{}`), plus `suggested_peers.txt` / `suggested_peers_test.txt` bootstrap lists.

> Runtime artifacts are gitignored and must not be committed: `wallet.der`, `*.db`, `*.log`,
> `config_custom.txt`, `heavy3a.bin`, `static/*.db*`.
