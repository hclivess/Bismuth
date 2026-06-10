# doc/26 — Post-fork storage rearchitecture (no SQLite)

> Status: **design done; stage 1 (shielded store on LMDB) ✅ shipped.** The endgame of doc/16: retire the
> 2014-era SQLite trio **post-fork** and make a single LMDB store canonical. Consensus serialization stays
> frozen (same block hashes, same validation); legacy SQLite keeps working **pre-fork** for old peers.

## 1. What's there today — the "H1/H2/H3" architecture, and why it isn't ideal

`DbHandler.__init__` opens **three SQLite connections across two files**, plus an index DB:

| handle | file | role |
|--------|------|------|
| `hdd` / `self.h` | `ledger.db` | the full transaction history |
| `hdd2` / `self.h2` | `hyper.db` | hyperblocks (periodic balance roll-ups + recent full blocks) |
| `conn` / `self.c` | `ledger_ram_file` *or* `hyper.db` | the "working" cursor — its source DEPENDS ON `ram` mode |
| `index` | `index.db` | token / alias indexes |

The non-idealities (the reasons to rearchitect):

1. **Two representations of one chain.** The full ledger and the hyperblock roll-up both describe history and
   must be kept consistent. The roll-up *mutates* history (collapses old txs into synthetic `Hyperblock`
   balance rows) and is **not integer-storage-safe** (`chain_ops.py:95`).
2. **Cross-file lockstep machinery.** `ledger.db` and `hyper.db` are written by separate connections, so a
   crash between writes splits them. The fix is a **dual `commit_marker` + `ATTACH`** crash-recovery contract
   (the long comment at `dbhandler.py:81`) reasoning about WAL's "atomic per file, not across files" caveat
   and a provable "≤ 1 block gap." ~39 references to that machinery in `dbhandler.py` alone — complexity that
   exists ONLY because there are two files to keep in step.
3. **Mode-dependent working cursor.** `self.c` reads `ledger_ram_file` in `ram` mode but `hyper.db` otherwise,
   so "where does `self.c` read from" is non-obvious and a source of subtle bugs.
4. **SQLite's ceilings.** Single writer + file locks; WAL checkpoint stalls; `ATTACH` atomicity caveats; no
   concurrent writers; sluggish on a multi-GB ledger (doc/16's own assessment). The legacy socket layer's
   serial pipeline compounds it.

## 2. The target — one LMDB store, canonical post-fork

A **single LMDB environment** (`block_store.BlockStore`, already in tree as an additive shadow) becomes the
canonical store post-fork. Everything else is a **derived projection** in its own LMDB env, rebuildable from
the block store. LMDB gives ACID, MVCC (lock-free concurrent readers for the REST API), ordered keys (range
scans for sync), and a single fast writer — no `ATTACH`, no WAL checkpoints, no cross-file lockstep.

```
                          ┌────────────────────────────────────────────┐
   canonical  ──────────► │  block store (LMDB)  height → {hash, txs}   │  (pubkey-deduped, ordered)
                          └───────────────┬────────────────────────────┘
                                          │  rebuildable projections (each its own LMDB, height-keyed)
        ┌──────────────┬──────────────────┼───────────────────┬─────────────────┐
   balances (LMDB)  tokens/aliases   shielded notes/keyimages   VM state        headers/checkpoints
                                     (stage 1, this doc)        (already LMDB)   (fast sync, replaces hyper)
```

Key properties:
- **Atomic block commit.** A block + all derived-index updates commit in ONE LMDB write txn (or each index
  rebuilds from the store). No `commit_marker`, no `ATTACH`, no two-file gap — crash recovery is LMDB's own.
- **Height-keyed rollback.** Each projection stores a `(height, key)` index so a reorg is a range-delete of
  `height ≥ H` plus, where needed, a running-total adjustment — the same shape `ShieldedState.rollback_under`
  and `vm_engine.rebuild` already use. (The reorg-completeness fix in doc/25 — every store rolled back on
  every path — carries straight over.)
- **No hyperblocks-as-a-second-file.** Fast bootstrap is the LMDB store (compact, deduped) + REST
  headers-first / parallel block fetch (doc/15, already started) + an optional **balance-snapshot projection**
  inside the SAME env (a derived checkpoint, not a lockstep second file).
- **Consensus frozen.** Block hashing / signing bytes are unchanged (`bismuth_serialize`); storage is an
  internal concern behind that boundary. The same blocks produce the same hashes.

## 3. The seam — a storage interface, two backends

The node accesses storage through a thin interface (`get_block(h)`, `get_tx(sig)`, `balance(addr)`,
`append_block(...)`, `rollback(h)`, range scans) with two implementations:
- **`SqliteBackend`** — wraps today's `DbHandler` (pre-fork + compatibility; unchanged consensus).
- **`LmdbBackend`** — the block store + LMDB projections (post-fork canonical).

Post-fork (`node.fork_height`) routes to `LmdbBackend`; pre-fork stays on SQLite. This is the strangler-fig
path: introduce the seam, move reads, move writes, then delete the SQLite trio. It also lets the two run
**side by side** during migration with a cross-check (the block store already verifies against SQLite).

## 4. Migration stages (each shippable, gated, test-green)

1. **Derived indexes → LMDB.** Move each rebuildable projection off SQLite, mirroring `vm_state`. **Shielded
   notes/keyimages/flows is stage 1 (this change)** — it is post-fork-only, self-contained, and the active
   "no SQLite post-fork" offender; it becomes the reference pattern for balances + tokens.
2. **Reads via the seam.** Route block/tx/balance reads through the interface; post-fork → LMDB.
3. **Writes via the seam.** The digester appends to the block store + LMDB projections atomically; the
   `commit_marker`/`ATTACH` lockstep is deleted (one store, one txn).
4. **Sync without hyperblocks.** Headers-first + parallel REST block fetch into the LMDB store; the balance
   snapshot becomes a derived checkpoint. `hyper.db` retired.
5. **Delete the SQLite trio** post-fork. `DbHandler` becomes `SqliteBackend` for the pre-fork compat window
   only, then removed when the network is fully past hf2.

## 5. Done / in progress
- ✅ Block store (LMDB), pubkey-deduped, reorg-safe, verified against SQLite (`block_store.py`).
- ✅ VM contract state on LMDB (`vm_state.py`), rebuildable, reorg-safe.
- ✅ Dynamic-fee congestion signal reads the **block store**, not SQLite (`block_store.recent_block_weights`).
- ✅ **Stage 1: shielded notes/keyimages/flows store on LMDB** (`shieldedv1.ShieldedState`) — replaced the
  SQLite sidecar with an LMDB env (sub-DBs `notes`/`notes_h`/`kimg`/`kimg_h`/`flows`/`meta`), SAME API so
  consensus (`validate_block`/`apply_block`) is untouched. Height-ordered secondary keys → reorg rollback is
  a range delete; ops are buffered per block and flushed atomically in one txn at `commit()`. `open_state_for`
  auto-removes a stale pre-LMDB SQLite file (the sidecar is post-fork-only + rebuildable, so nothing canonical
  is lost). 33 shielded/RingCT tests green (29 node-free + 4 live, incl. the confidential lifecycle).

### Next stages (in order)
- **Stage 2: balance index + token/alias indexes → LMDB**, same pattern (height-keyed, rebuildable).
- **Stage 3: reads via a storage seam**; post-fork routes block/tx/balance reads to LMDB.
- **Stage 4: writes via the seam**; delete the `commit_marker`/`ATTACH` lockstep (one store, one txn).
- **Stage 5: sync without hyperblocks** (headers-first + parallel REST into LMDB; retire `hyper.db`), then
  remove the SQLite trio post-fork.
