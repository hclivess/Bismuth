# doc/27 — Modern plugins: tokens & aliases out of the core

> Status: **framework + the tokens_aliases plugin ✅ shipped (config-gated).** Tokens and aliases are no
> longer node-core features — they are an optional, self-contained plugin owning its own LMDB storage. This
> is the logic half of doc/26 stage 2 (which moved the *storage* off SQLite): doc/26 gave tokens/aliases a
> separate LMDB store; doc/27 moves the *code* out of the core into a plugin that reacts to the block
> lifecycle. Inspired by the 2018 [BismuthPlugins](https://github.com/bismuthfoundation/BismuthPlugins)
> hook system, modernised into a typed class interface.

## 1. Why

Tokens and aliases were always a **derived side-index** of the ledger — their own `index.db`, never
consensus (doc/26 §1). But the *code* was scattered through the core: `tokensv2`/`aliases` scanners called
from the digester, hard-coded `tokensget`/`aliasget`/`aliasesget`/`addfromalias` branches in the wire-command
loop, hard-coded `/api/tokens` + `/api/token` routes in the REST router, and rollback hooks in `dbhandler`.
The directive: **post-fork the node core carries no token/alias code at all** — they become an optional
plugin, no SQLite. A node that doesn't want tokens simply doesn't load the plugin.

## 2. The framework (`plugin_base.py` + `plugins.py`)

The legacy `PluginManager` discovers plugins by magic function names (`action_block`,
`filter_extra_commands_prefixes`, …). That still works (BismuthPlugins compat). On top of it we add a typed
**class** interface:

```python
class BismuthPlugin:                       # plugin_base.py
    name = "..."
    def setup(self, ctx): ...              # ctx: PluginContext (logger, private data_dir, ledger_path, config)
    def backfill(self): ...                # build derived state for history already on disk (mid-chain enable)
    def on_block(self, height, txs): ...   # a committed block (txs = the 12-field ledger rows)
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

A plugin is **consensus-inert**: `on_block` runs AFTER a block is committed and only writes the plugin's own
store; it can never change a block hash or accept/reject a tx.

## 3. The tokens_aliases plugin (`plugins/tokens_aliases/`)

Owns a `token_index.TokenIndex` — the isolated LMDB store from doc/26 (sub-DBs for the token registry,
materialized credit/debit, the address↔token reverse index, alias maps, height journals; **no SQLite**),
namespaced per ledger (`tokenindex-<ledger>`) so a regnet run can't bleed into a mainnet node.

| Concern | How the plugin does it |
|---|---|
| **Index a block** | `on_block` two-pass scans the block's txs: issuances first (so a same-block transfer sees its token), then transfers (exact overspend rule: credit `< h`, debit `<= h`) and `alias=` registrations. |
| **Backfill history** | `backfill` scans the ledger from each anchor via `ctx.scan_ledger_operations` (read-only) when enabled mid-chain — idempotent (registry/txid dedup). |
| **Rollback** | The store is registered as the `token_index` service → `node.token_index`; the node's existing `dbhandler.tokens_rollback`/`aliases_rollback` seam rolls it back on **every** reorg path (reliable, unlike the best-effort `rollback` action hook). |
| **Queries** | `peer_commands` serve `tokensget`/`aliasget`/`aliasesget`/`addfromalias`; `rest_routes` serve `/api/tokens` + `/api/token/<name>` straight from the store. |

## 4. Wiring & the pre-fork → post-fork handover

The plugin is enabled by the `token_index` config flag (default off; on in the regnet test config). The node
core was made to **defer** to it:
- `node.plugin_manager.start(node)` (in node bootstrap) opens the store and sets `node.token_index`.
- The digester calls `dispatch_block` per committed block (in `execute_block_hooks`).
- `tokensv2.tokens_update` / `aliases.aliases_update` become **no-ops** when `node.token_index` is set — the
  plugin owns indexing; otherwise the legacy SQLite `index.db` path runs unchanged.
- The wire loop and REST router consult the plugin's command/route tables; the legacy core handlers remain as
  the fall-through for the no-plugin (legacy `index.db`) path.

So **pre-fork** a node can run either path (legacy SQLite, or the plugin via `token_index=True`), and
**post-fork** the plugin is the only token/alias mechanism — the legacy core handlers + `index.db` are then
removed with the rest of the SQLite trio (doc/26 stage 5). Mainnet today (flag off, pre-fork) is untouched.

## 5. Tests
- `tests/test_tokens_aliases_plugin.py` — node-free: enable-gating, `on_block` token lifecycle, the same-block
  no-re-spend rule, alias first-claimant, the plugin's REST routes, and `backfill` from a temp ledger.
- `tests/test_token_index.py` — the LMDB store itself (doc/26).
- Live regnet (with `token_index=True`): `tests/test_explorer_endpoints.py` exercises the plugin's `on_block`
  + its `/api/tokens` / `/api/token` REST routes; `tests/test_api.py` the `tokensget`/`aliasget` reads; the
  rollback/reorg suite drives the plugin store through the seam.

## 6. Next
- Move the `tokensget`/`aliasget` wire commands and `/api/tokens` REST fully onto the plugin post-fork (delete
  the core handlers) — they are already registered by the plugin; the core ones are just the legacy
  fall-through.
- Optional: split into separate `tokens` and `aliases` plugins (the store already keeps their sub-DBs
  independent) and teach the alias projection the newer `alias:register` operation format (`aliasesv2`).
