# doc/27 — Modern plugins: tokens & aliases out of the core

> Status: **framework + the tokens_aliases plugin ✅ shipped, always on.** Tokens and aliases are not
> node-core features — they are a self-contained plugin owning its own LMDB store (`tokenindex-<ledger>`).
> The plugin is the node's only token/alias path. Inspired by the 2018
> [BismuthPlugins](https://github.com/bismuthfoundation/BismuthPlugins) hook system, modernised into a typed
> class interface.

## 1. Why

Tokens and aliases are a **derived side-index** of the ledger, never consensus. Their code used to be
scattered through the core: scanners called from the digester, hard-coded `tokensget`/`aliasget`/
`aliasesget`/`addfromalias` branches in the wire-command loop, hard-coded `/api/tokens` + `/api/token` routes
in the REST router, and rollback hooks in `dbhandler`. The core now carries none of that logic: it is one
plugin reacting to the block lifecycle, with its own LMDB store.

## 2. The framework (`plugin_base.py` + `plugins.py`)

The legacy `PluginManager` discovers plugins by magic function names (`action_block`,
`filter_extra_commands_prefixes`, …). That still works (BismuthPlugins compat). On top of it we add a typed
**class** interface:

```python
class BismuthPlugin:                       # plugin_base.py
    name = "..."
    def setup(self, ctx): ...              # ctx: PluginContext (logger, private data_dir, ledger_path, config)
    def backfill(self): ...                # build derived state for history already on disk (mid-chain enable)
    def on_block(self, height, txs): ...   # a validated block, before the ledger commit (txs = the 12-field ledger rows)
    def on_rollback(self, height): ...     # a reorg dropped blocks at >= height
    def services(self) -> dict: ...        # objects to expose to the node (e.g. {"token_index": store})
    def peer_commands(self) -> dict: ...   # {command: handler(data, socket)}  — extra wire commands
    def rest_routes(self) -> dict: ...     # {(method, (seg,...)): handler(route, query)} — extra REST endpoints
```

A loaded plugin module exposes a `PLUGIN` instance. The manager (`plugins.PluginManager`, extended):
- `start(node)` — once the net type + ledger path are finalized and the ledger is readable, builds each
  plugin's `PluginContext`, calls `setup` → registers `services` → `backfill`, and collects the
  `peer_commands` + `rest_routes` tables.
- `dispatch_block(height, txs)` / `dispatch_rollback(height)` — fan the block lifecycle to the plugins.
- `get_service(name)` — fetch a registered service (the node grabs `token_index`).
- `peer_command_handler(data)` / `rest_handle(method, route, query)` — dispatch a wire command / REST route
  to whichever plugin owns it (REST supports a trailing `"*"` wildcard segment, e.g. `("token", "*")`).

A plugin is **consensus-inert**: `on_block` runs after the block is validated and its hash computed, in
`execute_block_hooks` (`digest.py`), ahead of the ledger's `commit_block`. `dispatch_block` swallows every
plugin exception so an `on_block` failure can never reject the block, and the plugin only writes its own
LMDB store — it can never change a block hash or accept/reject a tx.

## 3. The tokens_aliases plugin (`plugins/tokens_aliases/`)

Owns a `token_index.TokenIndex` — an isolated LMDB store (sub-DBs for the token registry, materialized
credit/debit, the address↔token reverse index, alias maps, height journals),
namespaced per ledger (`tokenindex-<ledger>`) so a regnet run can't bleed into a mainnet node.

| Concern | How the plugin does it |
|---|---|
| **Index a block** | `on_block` two-pass scans the block's txs: issuances first (so a same-block transfer sees its token), then transfers (exact overspend rule: credit `< h`, debit `<= h`), the legacy `alias=` first-claimant registrations, and the alias **evolution ops** `alias:register` / `alias:transfer` (`recipient:alias`, owner-only) / `alias:free` (owner-only) — mutable ownership with reorg-safe undo records. |
| **Backfill history** | `backfill` scans the ledger from each anchor via `ctx.scan_ledger_operations` (read-only) when enabled mid-chain — idempotent (registry/txid dedup). |
| **Rollback** | The store is registered as the `token_index` service → `node.token_index`; the node's existing `dbhandler.tokens_rollback`/`aliases_rollback` seam rolls it back on **every** reorg path (reliable, unlike the best-effort `rollback` action hook). |
| **Queries** | `peer_commands` serve `tokensget`/`aliasget`/`aliasesget`/`addfromalias`; `rest_routes` serve `/api/tokens` + `/api/token/<name>` + `/api/token/tx/<address>` (dict-shaped token transfers for an address, newest first) straight from the store. The same store also backs the REST seam's `/api/token/tx/<address>`, `/api/alias/<name>` (resolve an alias to its current owner) and `/api/aliases/<address>` (all aliases owned by an address) via `node.token_index`. |

## 4. Wiring

The plugin is always loaded. The node core defers to it:
- `node.plugin_manager.start(node)` (in node bootstrap, once the ledger is open) opens the store and sets
  `node.token_index` from the plugin's `token_index` service.
- The digester calls `dispatch_block` per validated block in `execute_block_hooks` (`digest.py`) — after the
  hash/PoW check, before the ledger commit.
- `DbHandler.tokens_rollback` / `aliases_rollback` roll `node.token_index` back on every reorg path.
- The wire loop and REST router serve tokens/aliases from the plugin's command/route tables and
  `node.token_index`.

## 5. Tests
- `tests/test_tokens_aliases_plugin.py` — node-free: `on_block` token lifecycle, the same-block
  no-re-spend rule, alias first-claimant, the plugin's REST routes, and `backfill` from a temp ledger.
- `tests/test_token_index.py` — the LMDB store itself.
- Live regnet: `tests/test_explorer_endpoints.py` exercises the plugin's `on_block`
  + its `/api/tokens` / `/api/token` REST routes; `tests/test_api.py` the `tokensget`/`aliasget` reads; the
  rollback/reorg suite drives the plugin store through the seam.

## 6. Next
- Optional: split into separate `tokens` and `aliases` plugins (the store already keeps their sub-DBs
  independent).
