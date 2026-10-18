# 11 — Configuration reference

`options.Get().read()` loads the base config, overrides it with an optional custom config, then reads
`mandatory_message.json`. Unknown keys are ignored; every key is copied onto `node.*` at startup.

## Format: `config.toml` + `config_custom.toml`

The node is configured in **TOML**, in two layers:

| Layer | File | Notes |
|---|---|---|
| Base | `config.toml` | the node's live settings (gitignored); restored from the tracked `config.toml.example` template if it goes missing |
| Override | `config_custom.toml` | optional; each key here overrides the base. Skipped when `BISMUTH_IGNORE_CONFIG_CUSTOM=1` is set — the systemd mainnet service sets it, so a stray regnet `config_custom.toml` can never hijack a mainnet node |

Precedence: **`config.toml` → `config_custom.toml` → `mandatory_message.json`**. Env-var overrides
(`node.py`, `BISMUTH_*`) apply on top and are unchanged.

Values are **native TOML types**, taken as-is per the schema in `options.py` (`Get.vars`):

```toml
port = "5658"                                   # str (an int `port = 5658` is accepted and coerced to "5658")
version = "mainnet0022"                         # str
version_allow = ["mainnet0021", "mainnet0022"]  # list -> array
rest_api = true                                 # bool -> real true / false
rest_api_port = 5659                            # int
api_sync = true
api_sync_source = "185.100.232.5:5659"
tor = "managed"                                 # off | external | managed (doc/38)
light_ip = { "127.0.0.1" = "5658", "bismuth.live" = "5658" }   # dict -> inline table
```

Unknown keys are ignored. Every key is copied onto `node.*` at startup.

**Why TOML:** stdlib **`tomllib`** reads it on Python 3.11+ (on 3.10 the `tomli` package, installed by
`install_node.sh` / `requirements.txt`, provides the same API), so a consensus node carries no extra parser.
It also has none of YAML's type-coercion footguns (the Norway `no`→False problem, implicit
numeric/version coercion) — the right property for consensus-affecting flags like `fork_signal` and
`version_allow`.

### Migrating from `config.txt`

A pre-TOML `key=value` `config.txt` / `config_custom.txt` is migrated **automatically, once**, on the
first start that finds it without its TOML counterpart: the node writes `config.toml` /
`config_custom.toml` beside it, **verifies both files load to the identical Config** (nothing is written
otherwise), and renames the old file to `config.txt.migrated` / `config_custom.txt.migrated`. The
emitted TOML contains only the keys you actually set, not the backfilled defaults.

The same conversion by hand:

```bash
python3 scripts/migrate_config.py --check    # verify only: would config.toml load identically?
python3 scripts/migrate_config.py            # write config.toml from config.txt (refuses to clobber; --force to override)
python3 scripts/migrate_config.py --custom   # also convert config_custom.txt -> config_custom.toml
```

## Keys

| Key | Type | Meaning |
|---|---|---|
| `port` | str | TCP listen port (mainnet 5658); kept as a string by the loader (`options.py:21`) |
| `verify` | bool | full signature re-verification of the ledger at startup |
| `testnet` / `regnet` | bool | network selection |
| `heavy` | bool | require the Heavy3 `heavy3a.bin` file (false on regnet) |
| `version` | str | protocol version (forced to `mainnet0023` on mainnet at startup, regardless of `config.toml`) |
| `version_allow` | list | accepted peer protocol versions |
| `thread_limit` | int | thread budget (inbound throttled at ~2/3; workers below 3×) |
| `debug` | bool | re-raise exceptions in handlers |
| `debug_level` | str | log level (`WARNING`, `DEBUG`, …) |
| `purge` | bool | mempool purge flag |
| `pause` | int | sleep interval used throughout the sync loops |
| `ledger_path` | str | names the ledger (mainnet `static/ledger.db`); the LMDB environment is `blockstore-<name>` beside it (`static/blockstore-ledger.db`), and every per-ledger sidecar is namespaced by the same name |
| `ban_threshold` | int | warning points before a peer is banned |
| `nodes_ban_reset` | int | pool size under which the banlist auto-resets |
| `tor` | str | Tor mode `"off"` / `"external"` / `"managed"` (doc/38); a legacy boolean is still accepted |
| `allowed` | str/list | IPs (or `any`) permitted to issue privileged commands |
| `banlist` / `whitelist` | list | initial banned / never-banned IPs |
| `node_ip` | str | the node's own external IP |
| `light_ip` | dict | `{ip: port}` light/wallet servers |
| `reveal_address` | bool | expose the wallet address in status replies |
| `accept_peers` | bool | accept peer announcements |
| `mempool_allowed` | list | **unused** — declared (`options.py:49`) but read by no module |
| `terminal_output` | bool | echo logs to stdout |
| `log_color` | bool | ANSI-coloured console/journald log by level (`log.ColoredFormatter`; honors `NO_COLOR`); file logs stay plain |
| `mandatory_message` | list | exchange deposit address → required-memo note; the loader's type schema is `list` (`options.py:59`), but the built-in default and `mandatory_message.json` override (see below) are a **dict** — set it via the JSON file, not a `config.toml` key |
| `egress` | bool | relay blocks to peers (false = receive-only) |
| `heavy3_path` | str | path to `heavy3a.bin` |
| `gui_scaling` | str | wallet-GUI hint (unused by the node) |

After load, `genesis` is hardcoded to `4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed`.

## Modernization keys (consensus / sync / API — doc/17–18)

Declared in `options.py`, assigned onto `node.*` at startup. **Both halves are required**: a key that is
declared in `options.py` but never assigned onto the node object is unreachable, because the consuming code
reads `getattr(node, "<key>", <falsy default>)`. That was the state of `snapshot_serve`,
`bootstrap_p2p` and `bootstrap_p2p_peers` until 2026-08-11 — the whole doc/43 feature answered "snapshot
serving disabled" no matter what the operator configured. When adding a flag, wire it in `node.py` and then
**exercise the surface it gates**; a passing default is not evidence. All default to a safe value and, where
consensus-affecting, are **inert until the hf2 fork activates**. (`rest_api_proxy_ports` is the
exception: it is *not* in the `options.py` schema — the REST layer reads it via
`getattr(node, …, default)`, so it only takes effect when set as a `node.*` attribute.)

The ledger has **no storage knobs**: it is always the one LMDB environment (doc/05), the mempool is
always in memory, and tokens/aliases are always indexed by the `tokens_aliases` plugin.

| Key | Type | Meaning |
|---|---|---|
| `rollback_consensus` | bool | auto-recovery rollback (default **ON**) — replaces the rigid `rollback_depth`; a stuck tip is recovered by a reputation-gated deep rollback (`essentials.rollback_allowed`) |
| `rollback_consensus_threshold` | int | how far below consensus the local tip must lag before auto-recovery triggers |
| `rollback_consensus_min_peers` | int | minimum peers in consensus before an auto-recovery rollback is allowed |
| `rollback_consensus_min_reputable` | int | minimum **proven-reputation** peers required — anti-sybil gate so a fresh flood can't force a deep reorg |
| `rollback_depth` | int | legacy fixed rollback anchor (superseded by `rollback_consensus`) |
| `ledger_integer_amounts` | bool | store amounts as integer atomic units (`amounts.py`); default off on mainnet |
| `fork_signal` | bool | emit the `hf2` coinbase signal when generating blocks (upgraded miners) — asserts readiness for the WHOLE bundle, including the blake2b Heavy3 (doc/18-D) |
| `fork_window` / `fork_boundary` / `fork_bury` | int | hf2 activation parameters (signal window, round boundary, burial margin) — `/api/fork` reports status |
| `mine` | bool | run the built-in solo miner (`miner.py`): real Heavy3, embeds mempool txs, writes the hf2 coinbase (doc/21) |
| `rest_api` / `rest_api_port` | bool/int | the read-only REST API (doc/15) and its port; **on by default** (port 5659) — set `rest_api = false` to disable |
| `snapshot_serve` | bool | expose `/api/snapshot[/info]`, generated **on demand, DB-direct** from the live LMDB ledger (doc/43); **on by default** |
| `bootstrap_p2p` | bool | a fresh node fetches a tip-verified DB-direct ledger snapshot from **peers** (doc/43), else seeds genesis and syncs; **on by default** |
| `bootstrap_p2p_peers` | list | optional explicit `host:rest_port` snapshot sources |
| `sync_from_genesis` | bool | on a fresh ledger, skip the snapshot: seed genesis (`ledger_genesis`) and sync from block 1 (doc/30) |
| `rest_api_write` | bool | enable `POST /api/transaction` (tx submission over REST — the post-fork transport, doc/15); off by default so a read-only node stays read-only |
| `rest_api_proxy` | bool | enable `GET /api/proxy` (default **ON**) — a read-only, SSRF-guarded same-origin relay so an https explorer can browse http nodes (doc/15) |
| `rest_api_proxy_ports` | str/list | extra target ports the proxy may reach, beyond the built-in `80/443/5658/5659` + this node's `rest_api_port` (`rest_api.py:526`, `PROXY_DEFAULT_PORTS`); space/comma-separated. The allowlist stops the relay being used as an arbitrary-port scanner (audit M-1) |
| `rest_api_geo` | bool | enable the geolocated peer map behind `/api/stats/geo` (default **ON**); set false to suppress the outbound geo lookup |
| `rpc_bitcoin` / `rpc_bitcoin_port`, `rpc_ethereum` / `rpc_ethereum_port` | bool/int | enable the external bitcoind-compatible (`:8332`) / `eth_*` (`:8545`) JSON-RPC adapters for exchange/explorer/web3 tooling (doc/17); both default off |

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

> Runtime artifacts are gitignored and must not be committed: `wallet.der`, the LMDB environments
> (`static/blockstore-*`, `static/tokenindex-*`), `*.log`, `config.toml`, `config_custom.toml`, `heavy3a.bin`.
