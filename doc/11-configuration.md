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
| `version` | str | protocol version (forced to `mainnet0022` on mainnet) |
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

## `mandatory_message.json`

A map of exchange deposit address → human-readable note. Transactions sent to these addresses with a
trivial (`len ≤ 4`) openfield are rejected by the mempool, to stop users losing exchange deposits
that lack the required memo. Override the built-in defaults by editing this file.

## Peer files

Single-line JSON `{ip: port}`: `peers.txt` (mainnet), `peers_test.txt` (testnet), `peers_reg.txt`
(regnet, `{}`), plus `suggested_peers.txt` / `suggested_peers_test.txt` bootstrap lists.

> Runtime artifacts are gitignored and must not be committed: `wallet.der`, `*.db`, `*.log`,
> `config_custom.txt`, `heavy3a.bin`, `static/*.db*`.
