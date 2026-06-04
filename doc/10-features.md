# 10 — Feature layers: tokens, staking, plugins, regnet

These are built on the generic `operation`/`openfield` transaction fields and the `index.db` indexes;
none change base consensus.

## Tokens (`tokensv2.py`)

Custom tokens are issued and transferred via ordinary transactions:

- **`operation = "token:issue"`**, `openfield = "<name>:<total_supply>"`, recipient = issuer. Only the
  first issuance of a given (lowercased) name is accepted; coinbase rows (`reward != 0`) are excluded.
- **`operation = "token:transfer"`**, `openfield = "<name>:<amount>"`. Accepted only if the sender's
  computed token balance covers it.

`tokens_update(node, db_handler)` scans the ledger from the last indexed height and maintains
`index.db`'s `tokens(block_height, timestamp, token, address, recipient, txid, amount)`. `txid` is
the first 56 chars of the signature (or a blake2b-160 of the row for mirror txs). It is called from
`digest_block()` whenever a block contains a token operation, and fires the `token_issue` /
`token_transfer` plugin hooks.

## Staking (`staking.py`)

A **proof-of-concept** (header TODOs note it is not the last integrated version, and it is not wired
into the main node loop). It registers participants via `operation = "staking:register"` into
`index.db`'s `staking` table over a 10,000-block window, requires a 10,000 BIS minimum balance at the
registration block, and pays out proportional "fuel" tokens every 10,000 blocks (the direct-BIS
payout path is commented out). `balanceget_at_block` computes a historical balance including mirror
rows. Treat this module as experimental.

## Plugins (`plugins.py`)

`PluginManager` discovers any sub-directory of `./plugins/` containing an `__init__.py` and loads it
via `importlib`. Plugins implement hooks by defining module-level functions:

- `action_<hook>(params: dict)` — fire-and-forget; called for every loaded plugin.
- `filter_<hook>(params: dict) -> dict` — pipeline; must return `params` with all original keys
  intact.

Known hooks fired by the core: `init` (`{manager}`), `token_issue`, `token_transfer`, `status`,
`block`, `fullblock`, `diff`, `digestblock`, and the `extra_commands_prefixes` filter (which lets a
plugin register new socket command prefixes — this is how the hypernode companion exposes `HN_*`).
See the BismuthPlugins repository for examples.

## Regnet (`regnet.py`)

An isolated, deterministic local network for tests:

- fixed difficulty `REGNET_DIFF = 16`, port `3030`, fresh `static/regmode.db` + `static/index_reg.db`,
  empty `peers_reg.txt`; `init()` inserts a known genesis block and creates the index tables.
- **no live networking** — `ConnectionManager` skips `client_loop()` on regnet.
- blocks are minted **on demand**: a client sends `regtest_generate <n>`, and `generate_one_block()`
  finds a trivial nonce, pops up to `TX_PER_BLOCK = 2` mempool txs, signs the coinbase with the
  node's key, and calls `DIGEST_BLOCK` (a function pointer the node injects at startup).

The pytest suite and `tests/regnet_smoke.py` both drive a regnet node this way. The regnet test
config is `tests/config_custom.txt` (`regnet=True`, `heavy=False`, `port=3030`, `version=regnet`).
