# 16 — Database rework plan (design)

> Status: **phase 1 implemented; phases 2–7 are design / roadmap.** This captures a complete rework of the storage
> layer, which today is a 2014-era SQLite design that is sluggish and awkward. The guiding constraint
> is that **consensus must not change**: the same blocks must produce the same hashes and validate
> identically, and the legacy socket protocol ([06](06-networking-protocol.md)) must keep working for
> old peers. Storage is an *internal* concern and can be modernised freely behind a frozen
> consensus-serialization boundary.

## What's wrong today (see [05](05-database-and-ledger.md))

- **Text amounts.** `amount`/`fee`/`reward`/`timestamp` are stored as TEXT and manipulated as
  `Decimal` everywhere. This is slow, memory-heavy, and error-prone, and forces `quantize_eight`
  churn on every arithmetic op.
- **Storage == serialization == consensus.** The block hash is `sha224(str(tx_tuple_list) + prev)`
  using the legacy `'%.8f'`/`str()` forms. Because the *string* representation is baked into
  consensus, the storage layout has never been allowed to evolve.
- **O(history) balances.** A balance is a `SUM(amount+reward) - SUM(amount+fee)` over all of an
  address's history (or the hyperblock). There is no maintained balance/account index, so balance
  and validation queries get slower as the chain grows.
- **Hacks instead of schema.** Dev/HN rewards are stored as **negative-height "mirror" rows**;
  pruned history is collapsed into synthetic `address='Hyperblock'` rows. Queries must special-case
  these (`abs(block_height)`, `address != 'Hyperblock'`, …).
- **No migrations / versioning.** The schema is created ad-hoc (`genesis.py`, `regnet.py`,
  `static/migrate.py`); indexes are added opportunistically (`ensure_indexes`); there is no
  `schema_version` and no forward-migration path.
- **Whole-DB RAM mode.** `ram=True` streams all of `hyper.db` into memory at startup.
- **Hyperblock rollup is heavy.** `recompress_ledger()` recomputes balances by scanning ranges and
  rewriting the DB — expensive and run inline.

## Design principles

1. **Freeze a consensus-serialization layer.** Extract the exact legacy byte forms used for
   *signing* and *block hashing* into one small, frozen, characterization-locked module
   (`Transaction.to_buffer_for_signing()`, block-hash assembly). Once storage only talks to consensus
   through this layer, the on-disk representation can change without touching consensus.
2. **Separate representation from storage.** In memory and on disk, use integer atomic units
   (1 BIS = 100,000,000 units) and native types; convert to the frozen legacy form only at the
   consensus boundary and in legacy API responses.
3. **Behavior-preserving, replay-verified.** Every migration is validated by replaying the chain and
   asserting **identical block hashes** end-to-end (a consensus-equivalence test). No silent drift.
4. **Backward compatible.** Old peers keep the socket protocol; legacy `*json` responses keep their
   exact shapes (the [test vectors](../tests/) pin them). New clients use the REST API
   ([15](15-rest-api.md)).

## Phased plan

1. **Consensus-serialization extraction + characterization. ✅ DONE.** `bismuth_serialize.py` now
   holds the frozen signing-buffer and block-hash byte forms, and `digest.py`, `essentials.sign_rsa`
   and `mempool.merge` all route through it instead of spelling the bytes out inline. Locked by
   `tests/test_characterization.py` (`test_consensus_signature_buffer_is_frozen`,
   `test_consensus_block_hash_is_frozen`) and gated end-to-end by `test_ledger.test_db_blockhash`
   (which recomputes real block hashes). This is the safety net for everything below: storage may now
   change freely **as long as these bytes do not**.
2. **Integer amounts behind the boundary.** Store amounts as integers; convert at the consensus
   boundary and legacy API edge. Removes `Decimal`/text churn from the hot path.
3. **Schema versioning + migrations. ✅ DONE.** `db_migrations.py` applies idempotent, ordered
   migrations tracked via SQLite `PRAGMA user_version`; `node.add_indices` now routes through it
   (v1 = the historical TXID4 partial-signature + misc block-height indexes). Unit-tested in
   `tests/test_db_migrations.py`. This is the mechanism the phases below use to add tables/indexes
   safely.
4. **Maintained account-balance index.** A `balances(address, balance, last_height)` table updated on
   block apply and rollback, giving **O(1)** balance lookups instead of O(history) sums. Must be
   rollback-safe (updated under `db_lock`, reverted by `blocknf`/`rollback_under`, and consistent with
   the configurable/consensus-aware rollback in [14](14-known-issues-and-improvements.md)).
5. **Explicit reward & pruning model.** Replace negative-height "mirror" rows and `Hyperblock` string
   rows with explicit columns/tables (e.g. `block_type`, a reward table, an "account snapshot at
   height H" table). Internal only; consensus serialization unchanged.
6. **Rollup & sync optimization.** Make hyperblock rollup *incremental* (derive the snapshot from the
   balance index instead of rescanning ranges) and move it off the inline path. Add binary,
   range-addressable block storage so blocks stream fast — pairs with REST range/since endpoints for
   **parallel** sync (vs the serial socket pipeline), while the socket sync stays for old peers.
7. **Engine evaluation (optional, measured).** Modernise SQLite usage first (WAL, integer keys,
   prepared statements, covering indexes) and benchmark; only then consider an embedded KV
   (LMDB/RocksDB) for block bodies with SQLite kept for queryable indexes. Decide on data, not taste.

## Migration & rollout

- Ship a one-time migrator that reads the legacy DB and writes the new schema, then **replay-verifies**
  block hashes before switching over; keep a dual-read fallback during transition.
- Gate each phase behind the existing test suite plus new consensus-equivalence vectors; never merge a
  phase whose replayed hashes differ.
- New storage is opt-in until a release is validated on testnet/regnet and by independent replay.

## Risks

- The consensus-serialization bytes must be **byte-identical** forever — this is the one thing that
  cannot drift. Lock it with vectors first (phase 1) before any storage change.
- Balance-index correctness under deep rollbacks (interacts with [14](14-known-issues-and-improvements.md)).
- Migration of large mainnet ledgers (time/space); provide a verified snapshot path.
