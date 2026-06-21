# 16 — Database rework plan (design)

> Status: **phases 1 & 3 done; phase 2 live on regnet (integer storage behind `ledger_integer_amounts`, default off); 4 & 6 partial (safe read-side slices); 5 is design / roadmap.
> Phase 7 (engine evaluation) is SUPERSEDED: the decision is made — post-fork storage is a single
> LMDB store, designed and landing in staged slices in [26-storage-postfork.md](26-storage-postfork.md),
> which is the authoritative storage doc going forward.** This captures a complete rework of the storage
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

   > **Future hard fork — delete the type conversions.** Because the consensus signature and block
   > hash are computed over the legacy decimal *string* forms (`'%.2f'` / `'%.8f'`), even integer
   > storage must reconstruct those strings at the boundary (`bismuth_serialize`). Those conversions
   > are a kludge. A scheduled **hard fork** should change the consensus serialization itself to
   > sign/hash over native types (integer atomic units + a binary/struct transaction encoding);
   > afterwards storage, the boundary and the APIs are integer end-to-end and the string conversions
   > are removed entirely. The integer-storage migration here is the stepping stone toward that fork.
   >
   > *Concretely, the kludge looks like this:* a stored amount of `250000000` atomic units (2.5 BIS)
   > must be reconstructed as the string `'2.50000000'` (via `amounts.from_units`) before it is
   > signed/hashed — because feeding the raw integer straight through the legacy `'%.8f'` path yields
   > `'250000000.00000000'`, which does not match the signed bytes and fails verification. Every site
   > that performs one of these consensus/display string conversions (or guards one with a broad
   > `except`) is tagged `# HARDFORK (doc/16)` in the code — run `grep -rn "HARDFORK (doc/16)"` to find
   > them all: the consensus boundaries in `digest.py`, `mempool.py` and `node.verify`; the display-edge
   > reconstruction in `essentials.format_raw_tx`; and the O(history)-balance / `recompress_ledger` /
   > schema-sniff smells in `node.py` (plus `ledger_queries.py`). The hard fork signs/hashes native
   > integer units and deletes the lot.

   > **Transaction id.** Today a txid is the first 56 chars of the base64 signature
   > (`signature[:56]`), matched with `signature LIKE '<txid>%'` — an ad-hoc slice of the signature
   > itself. The **nado cryptocurrency** (github.com/hclivess/nado) instead uses a proper
   > fixed-length txid: `create_txid(tx) = blake2b_hash(json.dumps(tx))` — a 32-byte / 64-hex BLAKE2b
   > hash of the transaction content (signature excluded) — and the **signature signs the txid**
   > (`message = unhex(tx["txid"])`). Bismuth should adopt the same model: a bounded, fixed-length,
   > content-derived txid that is the canonical identifier across node, APIs and wallets, decoupled
   > from the signature. Since the txid would become what is signed, this is a consensus change — do
   > it together with the hard fork above.
   >
   > **PINNED (see [18-hardfork-hf2.md](18-hardfork-hf2.md) §A.1).** Finalized as the Ethereum-shape
   > single-sig model: `txid = blake2b(signature_buffer(timestamp,address,recipient,amount,operation,
   > openfield), digest_size=32)` (64-hex), the signed message becomes `unhex(txid)` (32 bytes),
   > signatures become 65-byte recoverable compact secp256k1 (hex), and the `public_key` field is
   > DROPPED for single-sig (signer recovered via `ecrecover(txid,sig)` → address). blake2b is kept
   > (not keccak/RLP). The content txid is **computed on read** (`essentials.format_raw_tx`, amount via
   > `amounts.ledger_value` so it is storage-mode agnostic) — there is **no `txid` DB column and no
   > migration v3**. Lookups shape-dispatch: a 64-char lowercase-hex query (`^[0-9a-f]{64}$`) resolves
   > the content txid by scanning post-fork rows; anything else uses the legacy `signature LIKE` prefix
   > match. Only ordinary single-sig secp256k1 takes the recoverable-signature path above; RSA,
   > ED25519, native multisig and shielded/RingCT **keep their existing legacy signing** post-fork
   > (multisig: explicit pubkeys + N-of-M over the frozen buffer — it does **not** sign the txid), yet
   > all post-fork txs still get the content-hash txid as their canonical id. Full producer/lookup map
   > in §A.1.
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
2. **Integer amounts behind the boundary. ◑ LIVE on regnet (default off); mainnet sweep pending.** `amounts.py`
   provides the canonical, exact converter (`to_units` / `from_units`, 1 BIS = 100_000_000 units),
   unit-tested (`tests/test_amounts.py`). `replay_verify.py` recomputes every block hash from stored
   rows through the frozen boundary in two modes — as-stored (chain integrity) and with every amount
   round-tripped through integer units — and `tests/test_replay.py` proves on a regnet chain that the
   integer round-trip changes **no** block hash. The storage migration now has its safety gate (run
   before/after, require zero mismatches; operators can run `python3 replay_verify.py
   static/ledger.db` on mainnet).

   **Finding — type standardization (do in the migration):** the harness exposed that the
   `transactions` columns use SQLite NUMERIC affinity, so string fields like `timestamp` / `amount`
   are coerced to float/int on read — lossy, and a source of balance-math drift. The migration must
   declare **explicit column types**: integer atomic units for `amount` / `fee` / `reward`, `TEXT`
   for `timestamp` and the text fields, so storage is lossless and consistent. Consensus bytes stay
   frozen in `bismuth_serialize`; the `'%.2f'` / `'%.8f'` strings are produced only at the consensus
   / legacy-API edge.

   **First bite done:** `migrate_amounts.py` is the offline, replay-gated column migration — it
   rebuilds `transactions` with explicit types and integer atomic-unit `amount` / `fee` / `reward`
   (`timestamp` and the text fields become explicit `TEXT`), and `tests/test_amount_migration.py`
   proves on a chained ledger that it preserves **every block hash**, stores true integers
   (1.5 BIS → `150000000`), and that integer-summed balances are correct. Cutover recipe:
   `replay_verify legacy.db` → `migrate_amounts legacy.db new.db` → `migrate_amounts --verify new.db`
   (expect 0 mismatches at each step).

   **Live cutover — infrastructure landed (flag-gated, default off).** A `ledger_integer_amounts`
   config flag (module flag `amounts.LEDGER_INTEGER`, set once at startup) gates integer-unit storage.
   The consensus-sensitive conversions are done and verified to be **exact no-ops when the flag is
   off** (full suite green, mainnet untouched): regnet builds the integer schema; `digest`,
   `dbhandler.dev_reward`/`hn_reward` write `to_units`; the balance reads
   (`essentials.ledger_balance3`, `node.balanceget`, `apihandler._get_balance`/`_get_received`,
   `mempool.merge`) and `essentials.format_raw_tx` convert with `amounts.ledger_value` / `from_units`.
   Helpers `amounts.to_decimal` / `ledger_value` are unit-tested.

   **Live cutover — DONE (regnet-enabled, default off).** The display-edge sweep is complete: every
   client-facing handler that emits amount/fee/reward now reconstructs the backward-compatible form
   through the `amounts` boundary — `essentials.format_raw_tx` (the linchpin for `blockstojson` /
   `blocktojsondiffs`, hence `api_getblockfromhash*` / `fromheight` / `addressrange` / `getblockrange`),
   `node.py`'s `addlist` / `listlim` / `addlistlimmir` / `blocklast` / `blockget` (and their `*json`
   siblings), and `apihandler.api_getblocksince` / range / recipients / `addresssince` /
   `gettransaction*`. Crucially, `display_amount` / `display_row` return **floats** in integer mode
   (matching the legacy NUMERIC-coerced output), which is load-bearing for the `reward == 0`
   mining/normal split in `blocktojsondiffs` — a string `'0.00000000'` would be `!= 0` and misclassify
   every normal tx. The optional full-chain `node.verify` was also fixed to rebuild the signing buffer
   via `from_units` in integer mode (it reads the *stored* column, so a raw integer `250000000` would
   have produced `'250000000.00000000'` and failed every signature). With `ledger_integer_amounts=True`
   in `tests/config_custom.txt`, the regnet node and the **whole suite** run on integer
   storage; `tests/test_replay.py` proves block hashes stay byte-identical, while
   `tests/test_integer_storage.py` (live on-disk `INTEGER` columns) and
   `test_api_getblockrange_classifies_normal_tx` (the `blocktojsondiffs` split) lock the cutover.
   Default stays off, so mainnet is untouched.

   **Left legacy (sweep before enabling on mainnet).** The hyperblock rollup `recompress_ledger` (sums
   `amount+reward` with bare `quantize_eight` and writes the collapsed balance as a decimal-string
   `address='Hyperblock'` mirror row) and the hypernode quick-balance / `ledger_queries.py` callers
   (must convert SUM results via `amounts.ledger_value`, not `quantize_eight`) are **not** integer-safe
   yet. Regnet/tests don't exercise pruning or hypernode balances, so these are knowingly unconverted
   and tagged `# HARDFORK (doc/16)`; they must be converted (and the mirror-row hack replaced, phase 5)
   before `ledger_integer_amounts` is ever set on a mainnet ledger.

   This unblocks the exact incremental balance index (phase 4) and the schema cleanup (phase 5).
3. **Schema versioning + migrations. ✅ DONE.** `db_migrations.py` applies idempotent, ordered
   migrations tracked via SQLite `PRAGMA user_version`; `node.add_indices` now routes through it
   (v1 = the historical TXID4 partial-signature + misc block-height indexes). Unit-tested in
   `tests/test_db_migrations.py`. This is the mechanism the phases below use to add tables/indexes
   safely.
4. **Account-balance acceleration. ◑ PARTIAL (safe slice done).** A process-wide, height-stamped
   balance cache (`balance_cache.py`, exposed as `DbHandler.balance_get`, used by REST `/balance`)
   memoizes the authoritative balance per `(address, chain-height)`, so repeated reads are O(1) and
   always identical to the authoritative computation; it auto-invalidates when the height changes
   (block or rollback). Gated by
   `tests/test_rest_api.py::test_rest_balance_matches_socket_and_is_cached`. The deeper, true
   first-touch O(1) **incremental credit/debit index** (updated on apply/rollback) is deferred until
   after phase 2 (integer amounts), so it can bit-match the legacy text/float precision exactly and be
   maintained safely.
5. **Explicit reward & pruning model.** Replace negative-height "mirror" rows and `Hyperblock` string
   rows with explicit columns/tables (e.g. `block_type`, a reward table, an "account snapshot at
   height H" table). Internal only; consensus serialization unchanged.
6. **Rollup & sync optimization. ◑ PARTIAL (read side done).** The REST API now serves
   `GET /api/blocks/since/{h}` and `GET /api/blocks/range/{start}/{end}` (positive-height blocks) for
   **parallel** HTTP fetching, while the serial socket sync stays for old peers
   (`tests/test_rest_api.py::test_rest_blocks_since_and_range`). Still to do: a client-side
   parallel-fetch syncer, compact binary block streaming, and making hyperblock rollup *incremental*
   (derive the snapshot from the balance index rather than rescanning ranges) and off the inline path.
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
