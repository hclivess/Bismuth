# 17 — Roadmap

> The plan for modernizing the Bismuth node. This is the high-level companion to the database deep-dive
> in [`16-database-rework-plan.md`](16-database-rework-plan.md); read that for the storage details.

## Guiding principles

1. **Consensus does not change.** The same blocks must produce the same hashes and validate
   identically. The exact signing-buffer and block-hash byte forms are frozen in `bismuth_serialize.py`
   and characterization-locked; everything else (storage, transport, APIs) is free to change behind
   that boundary.
2. **Replay-verified.** Every storage/representation change is validated by re-hashing the chain
   end-to-end through the frozen boundary (`replay_verify.py`, `tests/test_replay.py`) and must produce
   **byte-identical** block hashes. No silent drift.
3. **Incremental and reversible.** Small, test-gated changes — not a big abstract rewrite. New
   behaviour is opt-in (config-flagged, default off) until validated on regnet and by independent
   replay. Old peers keep working.
4. **Modernize at the edges, freeze the core.** New, clean modules at the seams; surgical edits to the
   legacy monoliths; the consensus core is touched only through the frozen layer.

## ✅ Done

- **Consensus-serialization freeze.** `bismuth_serialize.py` holds the frozen signing/block-hash byte
  forms; `digest`, `mempool` and signing all route through it. Locked by `tests/test_characterization.py`.
- **Schema versioning & migrations.** `db_migrations.py` applies ordered, idempotent migrations tracked
  by `PRAGMA user_version`.
- **Integer atomic-unit storage (phase 2).** `amounts.py` provides the exact decimal↔integer-units
  converter; `replay_verify.py` proves integer round-tripping changes no block hash; `migrate_amounts.py`
  is the offline column migration. The live cutover is **enabled on regnet** behind the
  `ledger_integer_amounts` flag (default **off**, so mainnet is untouched). Display/consensus edges all
  reconstruct the legacy decimal strings at the boundary.
- **Balance cache (phase 4, safe slice).** `balance_cache.py` memoizes the authoritative balance per
  `(address, height)`; auto-invalidates on height change.
- **Modern HTTP layer.**
  - Read-only **REST API** (`rest_api.py`): status, blocks (single / since / range), balance,
    transaction, address history, mempool, peers; a self-describing welcome index.
  - **Capability discovery** (`GET /api/capabilities`): reachability *is* the test — if a peer's API
    answers, it is REST-capable, and advertises its rest port + negotiable codecs.
  - **Parallel block fetch** (`rest_client.py`): concurrent, compressed `/api/blocks/range` chunks —
    the performant alternative to the serial socket sync. Fails soft → falls back to sockets.
  - **HTTP transport compression**: `gzip`/`br` via `Accept-Encoding` (codecs from `transport.py`,
    zero hard native deps); `?compress=none|gzip|br` override for explicit plaintext/codec.
  - **Version bump** `mainnet0023` as the modern-capabilities signal (not a consensus change —
    nothing in `digest`/`bismuth_serialize` gates on the version string).

## ◑ In progress / next

- **API-based sync.** Wire `rest_client.parallel_fetch` into the actual catch-up path so a node syncs
  over the HTTP API when a peer is REST-capable, instead of the serial, blocking, no-asyncio socket
  loop (`connections.py`) that stalls. The socket protocol stays for old peers; it is no longer where
  new capability is added. (Requires a careful REST-block → digester mapping and two-node validation.)
- **Incremental balance index (phase 4 deep).** A maintained O(1) credit/debit index, updated on
  apply/rollback, that bit-matches the authoritative computation. Depends on integer storage.
- **Explicit reward & pruning model (phase 5).** Replace negative-height "mirror" reward rows and
  synthetic `address='Hyperblock'` rows with real columns/tables; convert `recompress_ledger` and the
  hypernode/`ledger_queries` paths to integer units (currently left legacy, tagged `# HARDFORK`).

## 🗺️ Planned

- **Consensus hard fork.** Change the consensus serialization itself to sign/hash **native integer
  units + a binary/struct tx encoding**, deleting the `'%.8f'`/`'%.2f'` string reconstructions (every
  site is tagged `# HARDFORK (doc/16)` — `grep -rn "HARDFORK (doc/16)"`). Adopt a bounded,
  content-derived **txid** (nado-style: `blake2b(tx_content)`, the signature signs the txid) to replace
  the ad-hoc `signature[:56]` slice. After the fork, storage/boundary/APIs are integer end-to-end.
- **Retire the blocking socket protocol** for node-to-node traffic in favour of the HTTP API
  (parallel, compressed, non-stalling). Keep a compatibility bridge while the network upgrades.
- **Storage-engine evaluation (phase 7).** Modernize SQLite usage (WAL, integer keys, covering
  indexes), benchmark, then consider a KV store (LMDB/RocksDB) for block bodies while keeping SQLite
  for queryable indexes. Decide on data, not taste.
- **Repository reorganization & modularization.** Retire dead files (✅ done — moved to `attic/` by
  import-graph analysis) and break up the over-long modules behind **behavior-preserving, test-green**
  extractions. `node.py` (~2.2k lines, with a ~1080-line socket-command `handle()`) is the prime
  target: turn the `if/elif` command chain into a dispatch table of small handlers, and lift the
  bootup/init and ledger-maintenance functions into focused modules (most already take `node`
  explicitly, so they extract by dependency injection). Cuts done so far: the pure block→JSON formatters
  moved out of the 900-line `apihandler.py` into `block_format.py`; and the chain-maintenance cluster
  (`rollback`, `recompress_ledger`, `ledger_check_heights`, `blocknf`, plus the boot/validation
  `bootstrap`, `check_integrity`, `sequencing_check`) lifted out of `node.py` into `chain_ops.py` by
  DI, with `blocknf` re-exported so `worker.py` is unaffected; the mempool-aware `balanceget` moved to
  `balances.py`; and the bootstrap/init helpers (`setup_net_type`, `node_block_init`, `ram_init`,
  `initial_db_check`, `load_keys`, `add_indices`) lifted into `node_init.py` by DI (the consensus chain
  `verify` deliberately stays). **`node.py` is down from 2200 to ~1420 lines (−36%).** The remaining
  bulk is the ~1080-line socket-command `handle()`: its branches use `break`/`continue` against the
  connection loop and interleave consensus-critical sync, so turning it into a dispatch table needs a
  two-node test harness first (tracked above under "API-based sync"), not a blind rewrite. Each step
  keeps the flat-import layout working and the suite green.

## How we work

- Branch behaviour behind a config flag; keep it default-off until regnet + replay validate it.
- Never merge a change whose replayed block hashes differ.
- Test every change end to end — the suite launches a real regnet node; new behaviour ships with tests.
- Prefer surgical edits and small clean modules over sweeping abstract rewrites.
