# doc/36 — The engine-agnostic KV store seam (`kvstore.py`)

> Status: **✅ shipped.** `kvstore.py` is in tree; all **8** LMDB stores
> (`reward_chain`, `txid_index`, `balance_index`, `vm_state`, `token_index`, `shieldedv1`,
> `block_store`) plus `scripts/snapshot.py` construct through the `open_store(backend, …)` factory.
> Two real backends are live and tested: **`lmdb`** (the default, a thin passthrough) and
> **`sqlite-kv`** (always-available, stdlib `sqlite3`). A third, **`mdbx`**, is wired into the factory
> as a lazy/optional one-arg choice; only its adapter body is deferred until a binding is pinned.
> Tests: `tests/test_kvstore.py`. Recommendation: **stay on LMDB** (MDBX is a one-arg swap if ever wanted).
>
> Relationship to doc/26 (post-fork storage rearchitecture): this is the **DB-engine seam**, stage 1 of
> doc/26 framed as code. It does NOT change *which* data lives in LMDB vs SQLite — that is doc/26's
> staged migration. It changes only *how* the LMDB stores open their engine: one factory instead of 8
> hand-rolled `lmdb.open()` calls.

---

## 1. The problem — 8 direct `lmdb.open` sites, no factory

Before this seam, every post-fork KV store opened LMDB **directly**. Each module had its own copy of:

* `lmdb.open(path, subdir=True, max_dbs=…, map_size=…, readonly=…, lock=…, sync=…, metasync=…)`,
* a bespoke `env.begin(write=…)` transaction lifecycle, hand-written `cursor`/`set_range`/`iternext`
  scan loops, and
* one of **three divergent (de)serialization styles** — msgpack (block bodies, rewards, balances),
  raw bytes (txid→height, vm words, key images), and JSON (token/alias projection, shielded notes).

There was **no shared base class and no factory**. Consequences:

* Swapping the underlying engine (e.g. LMDB → MDBX) would have meant editing 8 + 1 files, each with its
  own subtly different open args (`block_store` forces `lock=True` even when readonly; `snapshot`
  opens with `lock=False`, `dbs=[]`; map sizes range from 2 GiB to 64 GiB).
* The "is the engine even swappable?" question had no answer you could *run* — there was no second
  backend, so the abstraction boundary was implicit and untested.
* Every store reinvented ordered range scans, with their own off-by-one risks at the bounds.

`kvstore.py` introduces **one small interface + an `open_store` factory** so the engine becomes a
single argument:

```python
store = open_store("lmdb",      path, dbs=["rewards"], map_size=2 * KVStore.GIB)
store = open_store("sqlite-kv", path, dbs=["rewards"])              # always available
store = open_store("mdbx",      path, dbs=["rewards"])              # the day a binding is pinned
```

After migration, the **only** surviving `lmdb.open()` call in the whole node is the one inside
`LmdbKVStore.__init__` (`kvstore.py:267`) — the single seam. Verified: a grep for `lmdb.open` across
`*.py` + `scripts/*.py` returns only `kvstore.py` (plus doc-comment mentions in the migrated stores).

---

## 2. The interface — `KVStore` / `KVTxn`

Two abstract classes (`kvstore.py:83-156`) define the surface, deliberately shaped to the node's
*actual* usage rather than to any one engine's full API.

### `KVStore` (the env / connection)

| method | contract |
|--------|----------|
| `open_db(name)` | returns an opaque **db-handle** for a named sub-db (idempotent; opens on first use) |
| `txn(write=False)` | a **context manager** yielding a `KVTxn`. `write=False` is a read snapshot (many concurrent); `write=True` is the single short-lived writer |
| `stat(db=None)` | `{"entries": n, …}` for a sub-db (or the whole env) read in a **separate snapshot** |
| `sync()` | flush durably to disk |
| `copy_to(dst_path, compact=True)` | consistent **online** copy of the whole store (snapshot/bootstrap primitive) |
| `close()` | close the env/connection. `KVStore` is also a context manager (`__enter__`/`__exit__` → `close`) |

`KVStore.GIB = 1024**3` and `KVStore.Codec = Codec` are exposed as conveniences so stores write
`map_size=2 * KVStore.GIB` and reuse the shared codec without a second import.

### `KVTxn` (the transaction handle)

| method | contract |
|--------|----------|
| `get(db, key) -> bytes \| None` | point read |
| `put(db, key, val)` | upsert |
| `delete(db, key) -> bool` | delete; returns whether a row existed |
| `range(db, start=None, end=None, reverse=False)` | **key-ordered** iterator of `(key, val)` over the half-open `[start, end)`; `reverse=True` walks high→low |
| `iterate(db, prefix=b"")` | ordered iterator over all keys (or all keys under `prefix`) |
| `count(db)` | entry count **as seen inside this txn** (reflects uncommitted puts — see below) |
| `drop(db)` | clear ALL entries, keep the (empty) sub-db — the drop-and-reindex rebuild primitive |

Two subtleties that the node depends on, and which the interface pins explicitly:

* **`count` inside a write txn.** `KVTxn.count` reflects uncommitted puts in the *current* write txn,
  unlike `KVStore.stat` which opens a separate read snapshot. `block_store` relies on this: it assigns
  each new dedup pubkey an id = `txn.count(self.pk)` *within the same write txn* it's inserting into
  (`block_store.py:84`), so consecutive new keys in one block get consecutive ids.
* **Keys are raw bytes, never reinterpreted.** The KV layer passes keys through untouched. Stores own
  their key encoding (big-endian height, txid bytes, `token\0party\0HQ`, …), which is what keeps
  ordered range scans **engine-independent**: both LMDB and SQLite compare keys with byte-wise
  (`memcmp`) lexicographic order, so a store's `set_range`/prefix scans return the identical sequence
  on either backend.

---

## 3. `open_store(backend, …)` — the factory

`kvstore.py:528-548`. A dict-dispatch factory:

```python
_BACKENDS = {"lmdb": LmdbKVStore, "sqlite-kv": SqliteKVStore,
             "sqlite": SqliteKVStore, "mdbx": MdbxKVStore}

def open_store(backend, path, *, dbs, map_size=2*KVStore.GIB,
               readonly=False, sync=True, lock=None): ...
```

* `dbs` — the list of named sub-dbs to open up front.
* `map_size` — LMDB/MDBX map size (ignored by `sqlite-kv`).
* `lock` — overrides LMDB's default locking (`None` ⇒ `not readonly`). Setting `lock=True` keeps a
  readonly reader registered in LMDB's reader table while a writer is active (no-op on `sqlite-kv`).
* An unknown backend raises a clear `ValueError` naming the valid choices.

This is the **single seam** every store constructs through, so swapping the engine is a one-arg change.

---

## 4. `LmdbKVStore` — the thin passthrough (default), and its measured perf-neutrality

`kvstore.py:259-300` (+ `_LmdbTxn` / `_LmdbTxnCtx`, `:160-256`). The LMDB backend is a **thin
passthrough**, designed so that wrapping LMDB costs essentially nothing:

* `_LmdbTxn` is `__slots__ = ("_t",)` holding the raw `lmdb.Transaction`. `get`/`put`/`delete`
  forward **directly** to it with no per-op allocation, copy, or row-object churn.
* `range`/`iterate` build a cursor **lazily** (only when a scan is requested) and forward py-lmdb's
  native `iternext(keys=True, values=True)` iterator. py-lmdb (no-buffers mode) already yields real
  `bytes`, so there is **no per-item copy**; the bound checks reuse those same bytes. The reverse path
  handles the `set_range` lands-on-`>= end` / step-back-to-`< end` edge correctly.
* `open_db` is done once at open; the txn classes are plain attribute holders.

Other LMDB-specific behaviour preserved through the wrapper:

* `lock` defaults to `not readonly`, but a store may force `lock=True` *even when readonly* so a
  separate **reader process** registers in LMDB's reader table and reads consistently while the node
  writes (`block_store`'s concurrent-reader integration test relies on exactly this).
* `copy_to` uses `env.copy(dst, compact=compact)` — an MVCC snapshot, consistent even mid-write (a raw
  `cp` would capture a half-written mmap); `compact=True` drops free pages so the bootstrap is small.
* `count(db)` = `txn.stat(db)["entries"]` (sees uncommitted puts).

**Measured overhead** (`tests/test_kvstore.py::test_perf_lmdb_overhead`, `:181-262`). The test runs a
20 000-op micro-benchmark of `get` and `range` through `LmdbKVStore` vs raw `lmdb` txn calls, with two
gates:

* **Primary (robust, not scheduler-sensitive):** a `tracemalloc` net-retained-allocation-per-`get`
  signal — asserts the wrapper retains **< 64 bytes/op** (it is `__slots__` + one attribute deref, so
  it accumulates nothing; transient value bytes come from lmdb itself, same as raw).
* **Secondary (best-of-N wall-clock, to shrug off noise on a busy box):** `get` overhead < 20 %,
  `range` overhead < 40 %. In practice the wrapper adds about one Python call frame — a few percent.

The conclusion the seam needed: **wrapping LMDB is perf-neutral**, so there is no cost to routing the
stores through the abstraction.

---

## 5. `SqliteKVStore` — the always-available swappability proof

`kvstore.py:406-501` (+ `_SqliteTxn` / `_SqliteTxnCtx`, `:304-403`). A second, **real** backend built
on stdlib `sqlite3` — no third-party binding required, so it is *always* available and makes
swappability something you can *run*, not just claim.

Design:

* **One `(key BLOB PRIMARY KEY, value BLOB) WITHOUT ROWID` table per named db.** Ordered scans use
  `ORDER BY key`; SQLite compares BLOBs with `memcmp`, the **same** lexicographic order LMDB uses, so a
  store's big-endian-height `set_range` and prefix `iterate` return the identical sequence on both
  backends. Prefix scans use a `[prefix, prefix++)` half-open range computed by `_prefix_upper`
  (handles the all-`0xff` "no upper bound" case).
* **A fresh connection per txn** (`_SqliteTxnCtx.__enter__` → `store._connect()`). This is what makes
  the KVStore "many concurrent readers + one writer" contract hold on SQLite too: WAL gives concurrent
  read snapshots; each connection is used only within its own txn/thread (no cross-thread sqlite
  errors); `BEGIN IMMEDIATE` + `busy_timeout=30000` serialize the single writer. A single shared
  connection could do *neither* (BEGIN-within-BEGIN, cross-thread errors) — proven by
  `test_sqlite_concurrent_reads` (`:265-288`), which opens overlapping read txns and a cross-thread
  read.
* `text_factory = bytes` so keys/values round-trip as raw bytes; `isolation_level=None` so BEGIN/COMMIT
  are driven explicitly; WAL + durability pragmas are set once on the file at creation.
* `copy_to` uses SQLite's **online backup API** (`src.backup(dst)`) — transactionally consistent even
  while a writer keeps working, the same guarantee `env.copy` gives LMDB; lands at `dst/kv.sqlite`.
* The directory layout mirrors LMDB's subdir convention (a dir holding one db file, `kv.sqlite`), so
  the **same caller path** works on both backends and test cleanup (`rmtree`) is identical.

`sqlite-kv` is the portable proof/fallback backend, **never the LMDB hot path**, so the per-txn connect
cost is acceptable.

---

## 6. `MdbxKVStore` — lazy/optional, and EXACTLY how to add MDBX

`kvstore.py:505-524`. MDBX is wired into the **factory** today (`open_store("mdbx", …)` dispatches to
it), but its adapter body is deferred until a binding is pinned. The class:

* imports `mdbx` **lazily** (inside `__init__`, not at module load) so the absence of a binding is a
  clean, actionable `RuntimeError` at construction — *not* an import crash when anything imports
  `kvstore`;
* if a binding *is* present, currently raises `NotImplementedError` (the wiring is intentionally
  deferred — the seam + factory choice exist now; LMDB/SQLite already cover swappability).

`test_mdbx_lazy_optional` (`:101-110`) pins this: with no binding, `open_store("mdbx", …)` raises a
`RuntimeError` naming the missing binding; with a binding installed, the test skips (adapter deferred).

**To add MDBX (the whole job):**

1. **Pin a binding** (e.g. libmdbx's Python binding) in requirements.
2. **Fill in `MdbxKVStore`** against the same `KVTxn` shape as LMDB. MDBX is API-compatible with LMDB's
   cursor model (`begin`/`cursor`/`set_range`/`iternext`/`stat`/`drop`), so this reuses
   `_LmdbTxn`'s logic almost verbatim — replace the env open and txn begin with the mdbx equivalents;
   `range`/`iterate`/`count`/`drop`/`copy_to` carry over directly.
3. **That's it.** The factory already routes `"mdbx"`; every store already takes `backend=`; the codec,
   key encoding, and on-disk byte layout are engine-independent. No store code changes.

In other words: the day MDBX is wanted, it is a one-arg `backend="mdbx"` swap plus filling one adapter
class — by design, the 8 stores never learn the engine changed.

---

## 7. The `Codec` — centralized (de)serialization

`kvstore.py:36-79`. One place for value (de)serialization, so a migrated store keeps a
**byte-identical** on-disk format:

* **Default: msgpack** (`packb(use_bin_type=True)` / `unpackb(raw=False, strict_map_key=False)`), with
  the **same JSON fallback** the stores used before if `msgpack` is absent. `Codec.backend` reports
  which is active. `strict_map_key=False` is set uniformly and intentionally — it only relaxes
  msgpack's rejection of non-str/bytes map keys; it does not change packed bytes, so on-disk parity is
  unaffected.
* **Height-key helpers** shared by every height-keyed projection: `hkey(h)` = `struct.pack(">Q", h)`
  (big-endian uint64 — lexicographic order == numeric), `unhkey(k)` the inverse.

Crucial nuance: **not every store uses the Codec for values.** Codec is used where the original store
serialized with msgpack (block bodies, rewards, balances). Stores whose values were *already raw bytes*
(txid→height, vm words, key images) or *JSON strings* (`token_index`, `shieldedv1`) write those bytes
**directly, NOT through the Codec** — precisely so their on-disk bytes match the pre-migration store.
Codec centralizes the *msgpack/JSON-fallback* decision and the *height-key* convention; it does not
impose msgpack on stores that never used it. (Every store does, however, use `Codec.hkey`/`unhkey` for
height keys, so the height-key encoding is shared.)

---

## 8. How all 8 stores migrated — byte-identical on disk

Each store changed in the **same mechanical way**: `lmdb.open(...)` → `open_store(backend, ...)`;
`self.env.begin(write=…)` → `self.store.txn(write=…)`; hand-written cursor loops → `KVTxn.range` /
`iterate`. Every store gained a `backend="lmdb"` constructor kwarg (default unchanged) and kept a
back-compat `self.env = getattr(self.store, "env", None)` for callers/tests that introspect the raw env
(lmdb backend only). The **public method surface and the on-disk byte format are unchanged** in all 8.

| store | file | sub-dbs | value encoding | notes |
|-------|------|---------|----------------|-------|
| **reward_chain** | `reward_chain.py` | `rewards` | `Codec.pack` msgpack list of `[sender, recipient, amount, mirror_hash]` | key = `Codec.hkey(height)`. The canonical parity reference (§9) |
| **txid_index** | `txid_index.py` | `txid` | raw 8-byte BE height (no Codec) | key = raw txid bytes; post-fork-only projection |
| **balance_index** | `balance_index.py` | `bal` | `Codec.pack` msgpack `[credit_units, debit_units]` | key = raw address bytes; bit-matches `ledger_balance3` in integer mode |
| **vm_state** | `vm_state.py` | `code`, `storage`, `balances` | raw bytes (no Codec): bytecode / BE-256 word / BE custody balance | keys raw (`addr` / `addr:`+BE-256 word); feeds `state_root` |
| **token_index** | `token_index.py` | 11 (`meta`, `tokreg`, `seen`, `cred`, `deb`, `addrtok`, `tokset`, `journal`, `alias_fwd`, `alias_rev`, `ajournal`) | JSON / decimal-string / `b""` (NOT Codec) | keys `token\0party\0HQ` etc; ordered prefix/range scans via `KVTxn` |
| **shieldedv1** | `shieldedv1.py` (`ShieldedState`) | 6 (`notes`, `notes_h`, `kimg`, `kimg_h`, `flows`, `meta`) | JSON / BE-height / decimal-string / `b""` (NOT Codec) | reorg = height-ordered range delete via `KVTxn.range` |
| **block_store** | `block_store.py` | 4 (`blocks`, `hashes`, `pk`, `pkr`) | `blocks`: `Codec.pack` msgpack `{"h":…,"t":[…]}`; others raw bytes | `lock=True` even when readonly; pubkey dedup id via `KVTxn.count` |
| **snapshot** | `scripts/snapshot.py` | (n/a — `dbs=[]`) | n/a | opens readonly `lock=False`, calls `KVStore.copy_to` (§4/§5) |

`scripts/snapshot.py`'s `snapshot_lmdb()` now opens through `open_store(backend, src, dbs=[],
readonly=True, lock=False)` and calls `store.copy_to(dst, compact=True)` — so the snapshot tool is
itself engine-independent (LMDB `env.copy` or SQLite online backup, by one arg).

---

## 9. How parity / swappability / byte-identity are tested

`tests/test_kvstore.py` proves all three properties of the seam:

* **(a) Byte-identical on disk** — `test_lmdb_on_disk_bytes_identical_to_direct_lmdb` (`:114-140`):
  drives the migrated `RewardChain` (via `open_store` lmdb), then reads the raw key/value bytes straight
  out of the LMDB env and asserts they equal bytes reconstructed from the store's *original*
  (pre-migration) convention (`Codec.hkey(height)` → `Codec.pack([...])`). The on-disk format is
  byte-for-byte unchanged → a running node reads its existing files, and the consensus/balance parity
  proofs in each store's own test suite still hold.
* **(b) Swappability** — `test_swappability_lmdb_equals_sqlite` (`:169-177`): runs the **same**
  non-trivial `RewardChain` workload (adds across heights, balance deltas, a reorg `rollback`) through
  `backend="lmdb"` and `backend="sqlite-kv"` and asserts the full result dict is **equal**. If results
  differed, the seam wouldn't be engine-independent. This is the "swappability is real, and runnable"
  proof.
* **(c) Perf-neutrality of the LMDB wrapper** — `test_perf_lmdb_overhead` (§4).
* **(d) Ordered semantics across both backends** — `test_crud_roundtrip`, `test_ordered_range_and_iterate`
  (ascending/descending/half-open/reverse-bounded), `test_prefix_iterate` (including the `0xff`
  no-upper-bound case), all **parametrized over `["lmdb", "sqlite-kv"]`**.
* **(e) SQLite concurrency contract** — `test_sqlite_concurrent_reads` (overlapping read txns +
  cross-thread read), `test_unknown_backend_raises`, `test_mdbx_lazy_optional`.

Beyond the seam's own tests, each migrated store's existing parity suite (e.g. `balance_index` vs
`ledger_balance3`, `block_store.verify_against_sqlite`, the shielded/token reorg tests) continues to
pass unchanged *because* the on-disk bytes are identical — that is the migration's safety property.

---

## 10. Recommendation — stay on LMDB; MDBX is a one-arg swap

**Stay on LMDB.** Reasons:

* It is the incumbent, already battle-tested as the post-fork store engine; the migration is
  byte-identical, so there is **zero data-format risk** and nothing to re-bootstrap.
* The wrapper is **measured perf-neutral** (§4), so the abstraction costs nothing.
* `sqlite-kv` already proves the seam is real and gives an always-available portable fallback for
  tooling/CI without a native binding.

**MDBX remains a one-arg swap.** MDBX (a maintained, API-compatible successor to LMDB with some
robustness/feature advantages) is wired into the factory; adopting it is "pin a binding + fill one
adapter class" (§6), with no store changes. The seam exists so this is a *choice*, not a rewrite — but
there is no present reason to take it.

---

## 11. What is NOT abstracted — and why (deferred, inflation risk)

The seam covers the **post-fork LMDB KV stores**. It deliberately does **not** abstract the two
consensus-critical SQLite touchpoints, which remain on SQLite as the source of truth:

1. **The consensus SQL reads.** The overspend/balance check `essentials.ledger_balance3` and the
   duplicate-signature replay scan `digest._signature_exists_in_ledger` still read the SQLite
   `ledger.db` directly. These have **proven-consensus-faithful LMDB shadows** — `balance_index`
   (bit-matches `ledger_balance3` in integer mode) and `txid_index` (the O(1) replay lookup) — but the
   *authoritative* read is still SQLite. A wrong balance index would be **inflation**, so doc/26 keeps
   SQLite authoritative and runs the LMDB projections in `shadow`+`parity_strict` mode (raising on any
   divergence, default `off` on mainnet) until they have baked long enough to flip. That flip is a
   **consensus** change, gated behind config flags and a soak period — not something the KV-engine seam
   should silently move. (See `storage_backend.py:18-20, 168-171`; doc/26 §3-4.)

2. **The `commit_marker` / `ATTACH` lockstep write path.** The canonical block *write* is still the
   legacy two-phase SQLite path (`DbHandler.to_db` → working store, then `db_to_drive` → `ledger.db`),
   kept consistent across the two files by the dual `commit_marker` + `ATTACH` crash-recovery contract
   (`dbhandler.py:81-147`, `dbhandler_write.py:249-376`, `chain_ops.py:264-296`) — the machinery whose
   cross-file split caused a long outage. `storage_backend.LmdbWriteBackend` already implements the end
   state (one LMDB store, one atomic txn per block, no `commit_marker`/`ATTACH`/two-file gap), but it
   does **not yet replace** the SQLite write: flipping it requires the consensus reads (above) to have
   migrated AND the LMDB block write to have been continuously cross-checked long enough to trust as
   canonical. That flip needs a **two-node harness** and is deferred (doc/26 "Next stages").

The boundary is principled: the KV-engine seam changes *how an engine is opened*, never *what is
authoritative for consensus*. Anything whose correctness is an inflation/double-spend risk stays on the
deliberately-conservative SQLite path until doc/26's staged, flag-gated, soak-tested migration moves it.

---

## 12. Recipe — how to migrate the next store

To put a new (or remaining-direct-`lmdb.open`) store behind the seam:

1. **Import the seam:** `from kvstore import open_store` (add `Codec, KVStore` if you serialize with
   msgpack or need `GIB`/`hkey`).
2. **Construct through the factory.** Replace `self.env = lmdb.open(path, subdir=True, max_dbs=N,
   map_size=…, …)` with:
   ```python
   self.store = open_store(backend, path, dbs=[...all sub-db names...],
                           map_size=…, readonly=readonly, sync=sync)   # + lock=True if a reader
   self.db = self.store.open_db("name")   # one per sub-db
   self.env = getattr(self.store, "env", None)   # back-compat for env-introspecting callers/tests
   ```
   Add a `backend="lmdb"` kwarg to `__init__`.
3. **Route every txn through `self.store.txn()`.** `with self.env.begin(write=W) as t:` →
   `with self.store.txn(write=W) as txn:`. Replace `t.get(k, db=db)` → `txn.get(db, k)`,
   `t.put(k, v, db=db)` → `txn.put(db, k, v)`, `t.delete(k, db=db)` → `txn.delete(db, k)`.
4. **Replace cursor loops with `range`/`iterate`.** `set_range`/`iternext` ascending scans →
   `txn.range(db, start=…, end=…)`; reverse/tip scans → `txn.range(db, reverse=True)`; prefix scans →
   `txn.iterate(db, prefix=…)`. Remember `range`'s `end` is **exclusive** (an old `h > end: break` loop
   becomes `end=hkey(end + 1)`). For the "next id = current count, in this write txn" pattern use
   `txn.count(db)`; for a full clear use `txn.drop(db)`.
5. **Keep bytes identical.** If the store serialized with msgpack, use `Codec.pack`/`unpack`; if it
   wrote raw bytes or JSON, **keep writing those exact bytes** (do NOT reroute through Codec). Use
   `Codec.hkey`/`unhkey` for big-endian height keys.
6. **Migrate `snapshot`/copy if the store is bootstrapped:** open `readonly=True, lock=False, dbs=[]`
   and call `store.copy_to(dst, compact=True)`.
7. **Prove it:** add (or extend) a test that (a) asserts on-disk bytes equal the pre-migration
   convention, and (b) runs the store's representative workload parametrized over
   `["lmdb", "sqlite-kv"]` and asserts identical results — mirroring `tests/test_kvstore.py`.
