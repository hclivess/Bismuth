# 10 — Feature layers: tokens, staking, plugins, regnet

These are built on the generic `operation`/`openfield` transaction fields and the `index.db` indexes;
none change base consensus.

> This page covers the **optional, consensus-neutral** feature layers. The newer features that *do*
> touch consensus (all signal-activated at the `hf2` fork) are documented separately: the
> [`hf2` hard fork](18-hardfork-hf2.md), the
> [post-quantum signers](20-post-quantum.md) — `polysign/` now ships ML-DSA and secp256r1 alongside the
> legacy RSA/ECDSA/ed25519/BTC/CRW signers, all behind `SignerFactory` — and
> [mining](21-mining.md): the Heavy3 PoW, the built-in solo miner (`miner.py`, `mine=True`), and the
> dual-algo (sha224 → blake2b) PoW swap, bundled into the single hf2 fork.

## Tokens & aliases (`tokensv2.py`; post-fork: the `tokens_aliases` plugin)

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

**Post-fork** this lives entirely outside the core: tokens and aliases are owned by the optional
`tokens_aliases` plugin (`plugins/tokens_aliases/`, see [doc/27](27-plugins.md)), which owns its own
LMDB store (no SQLite) and reacts to the block lifecycle. The node carries no token/alias code; the
plugin is gated by the `token_index` flag (inert on mainnet pre-fork). Alongside the legacy
first-claimant **`openfield = "alias=<name>"`** and the `token:issue` / `token:transfer` ops, the
plugin adds the alias-evolution ops for **mutable ownership**: **`alias:register`** (claim, first
claimant wins), **`alias:transfer`** (hand ownership to the recipient), and **`alias:free`** (release
the claim). Legacy `alias=` claims sit below these in registration order, so global ordering is
preserved.

> **Canonical txid, post-fork.** The canonical transaction id post-`hf2` is the **content-hash txid**
> — blake2b-256 of the same frozen pre-image consensus signs
> (timestamp/address/recipient/amount/operation/openfield). It is computed **on read**
> (`essentials.format_raw_tx`, with the amount normalised via `amounts.ledger_value` so it is
> storage-mode agnostic); there is **no new `txid` DB column and no schema migration**. Lookup is
> **shape-dispatched**: a 64-char lowercase-hex query is resolved as a content txid by scanning the
> post-fork rows, while anything else falls through to the legacy signature-prefix (`signature[:56]`)
> `LIKE` match. Pre-fork rows are byte-identical and historical txs keep their `signature[:56]` ids.

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

Action hooks fired by the core: `init` (`{manager}`), `token_issue`, `token_transfer`, `status`,
`block`, `fullblock`, `diff`, `digestblock`, `mined` (per accepted mined block), `sync`
(`syncing_from`), and `rollback` (during a reorg, from `chain_ops.py`). Filter hooks: `peer_ip` (lets a
plugin rewrite/ban an IP before connecting), `filter_rollback_ip`, and `extra_commands_prefixes` (lets a
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
- additional regnet-only socket commands (all in `regnet.command()`): `regtest_mine <n>` drives the
  **real** solo miner (`miner.py`) so the production mining path is exercised; `regtest_powcheck`
  returns dual-algo PoW samples; `regtest_rollback <below>` drives a real chain rollback. See
  [08](08-api-and-commands.md) for the full list.

The pytest suite and `tests/regnet_smoke.py` both drive a regnet node this way. The regnet test
config is `tests/config_custom.txt` (`regnet=True`, `heavy=False`, `port=3030`, `version=regnet`).
