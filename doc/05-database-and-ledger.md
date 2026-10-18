# 05 — Database & ledger

The whole ledger is **one LMDB environment**. `block_store.BlockStore` owns it; `dbhandler.DbHandler` is
the thin facade the node calls. Every block — its transaction rows, its difficulty and every derived
index entry — is written in **one LMDB write transaction**, and a reorg is **one** transaction. There is
no second file to keep in step, no commit marker and no reconcile pass: a crash leaves the ledger at a
clean block boundary.

## Location

The config key `ledger_path` (mainnet default `static/ledger.db`) *names* the ledger; the LMDB env lives
next to it as `blockstore-<ledger file name>` (`kvstore.store_path`), e.g.

| network | `ledger_path` | LMDB env |
|---|---|---|
| mainnet | `static/ledger.db` | `static/blockstore-ledger.db/` (`data.mdb`, `lock.mdb`) |
| testnet | `static/ledger_test.db` | `static/blockstore-ledger_test.db/` |
| regnet | `static/regmode.db` | `static/blockstore-regmode.db/` |

Per-ledger naming keeps networks from ever sharing a store. The env is opened **once per process**
(`block_store.open_ledger(path)` returns the shared handle); `node.ledger` and `node.block_store` are the
same object. Map size defaults to 64 GiB (sparse; LMDB only consumes what it writes). The mainnet chain is
~9.7 GB in this format.

## Sub-databases

All integer keys are big-endian, so LMDB's ordered keys give O(1) tips and ordered range scans. `s64` is
a height offset by 2**63.

| sub-db | key → value | purpose |
|---|---|---|
| `blocks` | u64 height → msgpack `{"h": block_hash, "t": [tx, …]}` | block bodies. Pre-fork: 11-field lists (row minus height). Post-fork: one `txrec` true-bytes blob per tx plus the per-block address dictionary `"a"` ([40](40-storage-truebytes.md)) |
| `hashes` | raw block-hash bytes → u64 height | hash → height lookup |
| `pk` / `pkr` | blake2b-32(pubkey) → id, id → pubkey | public-key dedup: each RSA key is stored once, rows carry its id |
| `diff` | u64 height → difficulty text | the difficulty each block's PoW was checked against |
| `sig` | `signature[:56] ‖ 0x00 ‖ s64 height ‖ u16 idx` → "" | legacy txid / signature lookups, duplicate-replay check |
| `addr` | `role ‖ u8 len ‖ address ‖ s64 height ‖ u16 idx` → "" | address history (role 0 sender, 1 recipient) |
| `bal` | address → 4 × i128 LE | credited amount, rewards, debited amount, fees — integer atomic units |
| `txid` | raw content txid → u64 height | post-fork content-txid index ([29](29-hf2-serialization-v2.md)) |
| `pkref` | address → u64 first-seen height ‖ pubkey | post-fork pubkey-by-reference registry ([40](40-storage-truebytes.md)) |
| `meta` | `schema`, `txcount`, `alloc` | schema version, transaction count, fixed balance allocations |

A balance is `credited + rewards − debited − fees`, read in O(1) from `bal` (`balance_units`,
`balance`). Coinbase maturity ([42](42-coinbase-maturity.md)) subtracts the recent rewards walked from
the `addr` index (`reward_units_since`).

## Rows and column types

A ledger row is the classic 12-field tuple:

```
block_height, timestamp, address, recipient, amount, signature,
public_key, block_hash, fee, reward, operation, openfield
```

Readers always receive rows with the ledger column types restored by `block_store.typed_row`:
`block_height` integer; `timestamp` / `amount` / `fee` / `reward` numeric (well-formed integer → `int`,
real → `float`); everything else text. The digester writes string fields; the typed form is what
consensus difficulty, sync serving and the APIs consume, whatever form a row was stored in. With
`ledger_integer_amounts` the monetary fields are stored as integer atomic units (1 BIS = 10**8);
`block_store.units` converts either storage mode to exact units.

## Write path

- **`commit_block(height, block_hash, rows, difficulty, fork_height)`** — `height` must be tip + 1. In one
  transaction: body + `hashes`, `diff`, `sig`/`addr` entries, balance deltas folded into `bal`, post-fork
  `txid` and first-seen `pkref`, and the `txcount` bump. `DbHandler.commit_block` is the digester's call.
- **`rollback(to_height, fork_height)`** — drops every block above `to_height` with all its index
  entries and reverses its balance deltas, in one transaction, and returns the removed rows (ascending,
  typed) for the mempool. `DbHandler.rollback_under(h)` drops `h` and above.
- **`reindex()`** — rebuilds every derived index from `blocks` (recovery / upgrade); the live node
  maintains them incrementally.

## Fixed allocations (`meta` "alloc")

`ledger_genesis.MAINNET_ALLOCATIONS` holds two balance credits that no block row carries: the concluded
**Development Reward** and **Hypernode Payouts** totals. Until block 4,380,000 those rewards were minted
locally every 10th block as synthetic rows; the rows are gone and their totals are fixed allocations in
integer units, so both addresses' balances equal the network's. `set_allocations` stores them under
`meta` "alloc" and credits the difference to `bal` in the same transaction; `reindex` re-applies them.
Every block they cover is anchored by the block-hash checkpoints (`validation_exceptions`). Testnet and
regnet carry none.

## Opening, bootstrap and upgrade (`node_init.open_ledger`)

1. **Fresh install** (no `data.mdb`, not regnet): unless `sync_from_genesis` is set, `chain_ops.bootstrap`
   streams a DB-direct LMDB snapshot of a peer's ledger ([43](43-p2p-snapshot.md)), verifying tip
   height + tip block hash; the digester still re-validates everything it syncs forward.
2. Open the env. If it has blocks and `meta` "schema" ≠ `block_store.SCHEMA`, the node logs the exact
   command and **refuses to start**:
   `python3 scripts/upgrade_ledger.py static/blockstore-ledger.db --anchor http://<rest-node>:5659`.
3. An empty ledger is seeded with the canonical genesis block (`ledger_genesis.rows()`,
   `chain_ops.seed_genesis`) and the chain is synced from peers ([30](30-genesis-sync-exceptions.md)).
4. The fixed allocations are applied if they differ from `ledger_genesis.allocations(node)`.

**`scripts/upgrade_ledger.py`** brings an older LMDB block store (blocks only) to the current schema
with the node stopped: it replays the difficulty history forward with `difficulty.retarget` from block
timestamps, anchored on the exact difficulties peers publish at `GET /api/stats/difficulty` (re-anchoring
at each, reporting exact matches vs mismatches), then runs `BlockStore.reindex` and stamps the schema.
Idempotent.

## `DbHandler`

`DbHandler(store, logger, node)` / `DbHandler.for_node(node)` wraps the process-wide store, so
constructing one per thread is free. It keeps the semantic helpers the node has always called:

| Group | Methods |
|---|---|
| tip | `block_height_max`, `block_height_max_diff`, `last_block_hash`, `last_block_timestamp`, `last_mining_tx`, `block_max_ram`, `difflast` |
| lookups | `block_height_from_hash`, `pubkeyget`, `annget`, `annverget`, `blocksync` (8-field sync form, ~500 KB cap), `latest_rows`, `address_rows`, `balance_get` |
| tokens / aliases | `addfromalias`, `aliasget`, `aliasesget`, `tokens_user`, `tokens_rollback`, `aliases_rollback` — delegated to the `tokens_aliases` plugin's LMDB index ([27](27-plugins.md)) |
| writes | `commit_block`, `rollback_under` (`backup_higher` alias), `clear_caches` |

Concurrency is LMDB's: any number of MVCC read snapshots, one writer. Readers in another process (e.g.
`replay_verify.py`) see a consistent view while the node writes.

## Mempool

The mempool is in memory ([07](07-mempool.md)): an insertion-ordered dict keyed by signature with each
entry's merge time. It is not persisted; the digester re-validates every transaction it includes.

## Other LMDB stores

- `tokenindex-<ledger>` — the `tokens_aliases` plugin's token/alias index (`token_index.py`), rolled back
  with the ledger ([27](27-plugins.md)).
- `kvstore.py` — the small store interface both are opened through ([36](36-kvstore-engine-seam.md)).

## `quantizer.py`

`quantize_two` / `quantize_eight` / `quantize_ten` round to 2 / 8 / 10 decimal places with
`Decimal.quantize` (falsy input → zero). **8 dp is the monetary precision.**

## Other files

- `db_hashes.py` — a static dict of 67 known-good `"<height>-<timestamp>" → sha1` pairs from early
  mainnet (blocks 27,258–109,035), a corruption/fork reference.
- `replay_verify.py` — read-only checker that recomputes every block hash from the stored rows through
  `bismuth_serialize` (default env `static/blockstore-ledger.db`).
