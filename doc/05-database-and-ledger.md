# 05 — Database & ledger

All persistent state is SQLite. Every connection thread builds its own `DbHandler`
(`dbhandler.py`), which opens four connections.

## The four databases

| Logical | Attr / cursor | Default path (mainnet) | Role |
|---|---|---|---|
| full ledger | `hdd` / `h` | `static/ledger.db` | every transaction ever committed |
| hyperblocks | `hdd2` / `h2` | `static/hyper.db` | pruned/compressed ledger (working set) |
| working copy | `conn` / `c` | RAM URI, else `hyper.db` | the DB digest writes to; flushed to `h`/`h2` |
| index | `index` / `index_cursor` | `static/index.db` | aliases / tokens / staking secondary indexes |
| mempool | (own class) | `mempool.db` or RAM | unconfirmed txs — see [07](07-mempool.md) |

Comment from the source: `c` = hyperblock-in-RAM (or the hyperblock file when running hyperblocks
without RAM mode); `h` = ledger file (or a hyperblock clone in hyperblock mode); `h2` = hyperblock
file.

## Schemas

`ledger.db` / `hyper.db`:

```sql
transactions(block_height INTEGER, timestamp, address, recipient, amount, signature,
             public_key, block_hash, fee, reward, operation, openfield)   -- 12 cols
misc(block_height INTEGER, difficulty)
```

`index.db`:

```sql
aliases(block_height INTEGER, address, alias)
tokens(block_height INTEGER, timestamp, token, address, recipient, txid, amount INTEGER)
staking(block_height INTEGER, timestamp, address, balance)   -- only if staking is used
```

Conventions: negative `block_height` rows are dev/HN "mirror" reward rows; `address='Hyperblock'`
rows are balance-consolidation rows from pruning. Amounts/timestamps are text, quantized via
`quantizer` (below). Most balance queries must account for mirror rows (`block_height <= ?` plus the
negative range, or `abs(block_height)`).

## `DbHandler` (selected methods)

Constructor: `DbHandler(index_db, ledger_path, hyper_path, ram, ledger_ram_file, logger,
trace_db_calls=False)`. It applies PRAGMAs to all connections (`synchronous=NORMAL`,
`cache_size=-64000`, `temp_store=MEMORY`, `mmap_size=512 MiB`, `case_sensitive_like=1`; WAL on
`conn`) and keeps small per-handler caches (pubkey/alias/address/height).

| Group | Methods |
|---|---|
| tip / reads | `last_block_hash`, `last_block_timestamp`, `block_max_ram`, `block_height_max[_diff][_hyper]`, `block_height_from_hash`, `blocksync` |
| balances / lookups | `pubkeyget`, `difflast`, `annget`, `annverget` |
| index | `addfromalias`, `aliasget`, `aliasesget`, `tokens_user` |
| writes | `to_db` (batched block insert), `dev_reward`, `hn_reward`, `db_to_drive` (flush RAM→disk) |
| rollback | `backup_higher`, `rollback_under`, `rollback_to` (deprecated → `rollback_under`), `tokens_rollback`, `aliases_rollback` |
| low-level | `commit`, `execute`, `execute_param`, `fetchall`, `fetchone`, `ensure_indexes`, `clear_caches`, `close` |

Retry semantics differ by helper: `execute`/`execute_param` retry on generic errors but **break** on
`InterfaceError`/`IntegrityError`; `commit` retries forever. (These near-duplicate retry loops, also
present in `mempool`, `essentials.execute_param_c`, `ledger_queries` and `staking`, are a documented
consolidation target — see [14](14-known-issues-and-improvements.md).)

## Modes

- **RAM mode** (`ram=True`): `conn` is `file:ledger?mode=memory&cache=shared`; `ram_init()` streams
  `hyper.db` into it at startup. New blocks accumulate in RAM and `db_to_drive()` flushes to `h`/`h2`.
- **full vs hyperblock** (`full_ledger`): if false, `ledger.db` is deleted and replaced with a clone
  of `hyper.db` at startup (the node keeps only hyperblocks).
- **trace_db_calls**: logs every SQL statement via a per-connection trace callback (tagged
  INDEX/HDD/HDD2/CONN).

## Hyperblock pruning (`recompress_ledger`)

Blocks older than `depth` (default 15,000 from the tip) are replaced by one synthetic
`address='Hyperblock'` row per address that carries the address's end balance, then the original
rows and their `misc` entries are deleted and the DB is VACUUMed. `ledger_check_heights()` decides at
startup whether to recompress (when `hyper_recompress=True` and heights agree) or to roll all DBs
back to the minimum consistent height.

## `quantizer.py`

`quantize_two` / `quantize_eight` / `quantize_ten` round a value to 2 / 8 / 10 decimal places using
`Decimal.quantize` (falsy input → the zero Decimal). **8 dp is the monetary precision** — all BIS
amounts pass through `quantize_eight` before storage/comparison, which is why amounts are stored as
text rather than floats.

## Other storage files

- `db_hashes.py` — a static dict of 69 known-good `"<height>-<timestamp>" → sha1` pairs from early
  mainnet (blocks 27,258–109,035), a corruption/fork reference. Not a hash function.
- `ledger_queries.py` (`LedgerQueries`) — classmethod query helpers (balances, block-by-timestamp,
  hypernode register scans) used by plugins/hypernodes; takes a raw connection, not a `DbHandler`.
- `ledger_explorer.py` — an early standalone Tornado block explorer (port 5492); unmaintained.
