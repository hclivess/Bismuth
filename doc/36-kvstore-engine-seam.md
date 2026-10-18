# doc/36 — The KV store interface (`kvstore.py`)

> Status: **✅ in tree.** Every LMDB environment the node and its tools open — the ledger
> (`block_store.BlockStore`), the token/alias index (`token_index.TokenIndex`) and the pool server's store
> (`pool/pool_store.py`) — is constructed through one factory, `open_store(backend, …)`. Tests:
> `tests/test_kvstore.py`.

## Interface

```python
store = open_store("lmdb", path, dbs=SUBDBS, map_size=64 * KVStore.GIB, readonly=False, sync=True, lock=True)
db = store.open_db("blocks")
with store.txn(write=True) as txn:
    txn.put(db, key, value)
```

| member | meaning |
|---|---|
| `txn(write=False)` | a read snapshot — any number concurrently (LMDB MVCC) |
| `txn(write=True)` | the single writer; commits on clean exit, aborts on exception |
| `get` / `put` / `delete` | point operations on a named sub-db |
| `range(db, start, end, reverse)` | ordered key range scan |
| `iterate(db, prefix)` | prefix scan |
| `count(db)` / `drop(db)` | entry count (sees uncommitted puts) / clear a sub-db |
| `stat` / `sync` / `close` | env stats, fsync, close |
| `copy_to(dst)` | MVCC-consistent compacted online copy into a fresh directory |
| `copy_to_fileobj(f)` | the same image streamed to a file descriptor (`env.copyfd`) — the doc/43 DB-direct snapshot |

The LMDB wrapper is a thin passthrough: `KVTxn` forwards straight to the raw `lmdb.Transaction` with no
per-op allocation (overhead measured at a few percent in `test_kvstore.py::test_perf_lmdb_overhead`).
`lock=True` on a read-only open registers the process in LMDB's reader table, so a separate reader
(`replay_verify.py`, `upgrade_ledger.py`-style tools) sees a consistent view while the node writes.

## Backends

- **`lmdb`** — the engine. Opened with `subdir=True`, `max_readers=2048`, `sync`/`metasync` per the
  caller.
- **`mdbx`** — a lazy, optional factory entry. It raises a clear error until a libmdbx binding is pinned
  and its adapter wired against the same `KVTxn` shape; nothing selects it today.

## Codec and keys

`Codec.pack` / `Codec.unpack` are msgpack (JSON fallback when msgpack is absent; post-fork true-bytes
block bodies require msgpack). `Codec.hkey(h)` / `Codec.unhkey(k)` are big-endian u64 height keys, so
LMDB's lexicographic order is numeric order and the tip is the last key.

## Store paths

`store_path(ledger_path, name)` → `<dir>/<name>-<ledger file name>` (e.g. `static/blockstore-ledger.db`,
`static/tokenindex-regmode.db`). Every store is namespaced by its ledger, so mainnet, testnet and regnet
never share an environment.
