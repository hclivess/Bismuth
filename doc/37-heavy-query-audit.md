# doc/37 — Heavy-query audit, startup-scan & post-fork-growth remediation

Status: **fixes landed** (commits `196f8998` slow-resume, `97d12699` scan-narrowing). This page is the
standing reference for the node's heavy DB queries — what was optimized, what is deliberately left, and
what will degrade as the chain grows past the hard fork.

Motivation: a mainnet **restart took ~13 min to resume syncing**. The boot log split that into two
*unrelated* causes (the operator's first guess, hyperblock *recompression*, was **refuted** — it is
skipped in ~2 ms when `hyper_recompress=False`):

```
11:05:21.604  Hyperblock recompression skipped          <- ~2ms, not the cause
11:05:21.606  Indexing aliases
11:07:56.545  Chain sequencing test complete (ledger.db) <- ~2.5 min  full-ledger scan
11:08:54.872  Chain sequencing test complete (hyper.db)  <- ~1 min
11:08:54      REST/socket bind; "Testing peers" begins
11:18:32      first new block accepted                   <- ~9.6 min  dead-peer dial starvation
```

The unifying theme: **unbounded, full-history scans and from-zero rebuilds that have no height bound and
re-run on every boot (and every reorg)**. On the 23 GB ledger they are slow now and get worse as the
chain grows post-fork.

---

## 1. Index baseline (what actually exists on the live node)

From `dbhandler.py:220-225` + `db_migrations.py:25-32`, the `transactions` table has indexes on:
`address`, `recipient`, `block_height`, composite `(address, block_height)`, `(recipient, block_height)`,
and `substr(signature,1,4)` (**`TXID4_Index`**). **There is NO plain `signature` index and NO `openfield`
index** on the live node — the full `Signature Index` / `Openfield Index` exist only in the one-shot
`static/migrate.py`, which is **not guaranteed to have run on prod**. Every optimization below is framed
against this real index set (e.g. a bare `signature LIKE` is a true full-table scan; a `substr(signature,1,4)`
seek is not).

`misc` is the difficulty-history table `(block_height INTEGER, difficulty TEXT)`, one row per block.

---

## 2. Landed fixes

### 2.1 Slow sync-resume (`196f8998`)
- **`worker.py` — outbound dial timeout.** The sync worker called `s.connect()` with **no timeout**, so a
  dead/filtered peer hung it for the OS TCP SYN-retry window (~tens of s, up to ~127 s). Across the stale
  peers a node accrues while down, that was the ~9.6 min "stuck 1 block behind" phase. Now capped at
  `DIAL_TIMEOUT = 5 s` (clearnet only — Tor keeps its prior unbounded connect because onion-routed
  connects legitimately take longer), restored to blocking right after connect (the framed protocol
  re-asserts its own timeouts).
- **`chain_ops.sequencing_check` — bound the difficulty scan.** The `misc` scan hard-coded
  `block_height > 300000` (unbounded above, **ignoring the anchor**), re-reading every `misc` row from
  300000 to the tip on **every boot** — the bulk of the ~3.5 min startup cost. Now bounded to
  `max(300000, sequencing_last)`, the same recent-tail anchor the `transactions` scan already uses.
  `max()` preserves the original floor when no anchor file exists (no regression on a first/unanchored
  boot). **Safety:** deep history below the anchor is immutable and was validated on the boot that
  advanced the anchor; new gaps live at/after it, and the live autoheal loop (`node.py` ~1733) guards the
  tip. The corruption-recovery rollback branches are untouched.

### 2.2 Scan-narrowing (`97d12699`) — behaviour-preserving
- **`balance_index.rebuild_from_cursor` / `txid_index.rebuild_from_cursor`** — `SELECT *` → only the
  columns each actually consumes (5 and 7). These full-ledger rebuilds run at boot **and on every reorg**
  and were dragging every row's ~1 KB `public_key`+`signature` blobs through Python for fields they
  discard — the dominant boot-scan cost. The per-block **apply path is untouched**: a shared `_fold` /
  `_txid_fields` helper keeps it on 12-column rows, so on-disk bytes and the `ledger_balance3` bit-match
  parity proof hold. *(Verified: storage parity suite 18/18.)*
- **`rest_api.py` `/api/transaction` signature fallback** — a bare `signature LIKE 'prefix%'` is a
  full-table scan (and `LIMIT 1` does **not** help a miss — it scans to the end). Now seeks via
  `TXID4_Index`: `substr(signature,1,4)=substr(?1,1,4) AND signature LIKE ?2` for prefixes ≥ 4 chars
  (the exact pattern `digest._signature_exists_in_ledger` and the mempool SQL already use); sub-4-char
  prefixes fall back to the legacy scan.
- **`balances.balanceget`** — collapsed the **two** separate full-history `WHERE recipient = ?` scans
  (one for `amount`, one for `reward`) into one `SELECT amount, reward` — 3 ledger scans → 2. The two
  accumulators keep **separate `try/except` blocks**, so failure semantics are byte-identical.
  *(Verified: balanceget/tx-lookup/REST 40/40.)*

---

## 3. The consensus guardrail — do NOT "optimize" `ledger_balance3`

`essentials.ledger_balance3` (`essentials.py:234-240`) is a full-history aggregate **by design** — it is
the consensus overspend check and is under a directive to stay byte-identical to the `balance_index`
parity proof. **Do not add a height bound or change its SQL.** The intended fix already exists and is a
**config flip, not a code change**: flip `balance_index` from shadow → primary after the fork
(`balance_index_consensus`), so the O(1) maintained index serves the consensus balance. Until then the
full aggregate is correct and required.

---

## 4. Deferred / left as-is (with rationale)

| Site | Why not now |
|---|---|
| `vm_engine.rebuild` (`vm_engine.py:176`, `SELECT *` + `.fetchall()`) | Consensus **state-root** path; column-narrowing means remapping offsets `apply_block_rows` reads (incl. `signature` for calls + the per-deploy content txid) — a mistake diverges the root. And the `vm:` row set is **empty pre-fork** (zero current cost). Deferred as a careful post-fork follow-up: narrow + stream + count, gated on the `vm_state` parity tests. |
| `mempool` merge balance (`mempool.py:475-487`) | Already the good **2-scan** form (recipient + address, single-pass); no redundant scan to collapse. Deeper win is the same balance-index-primary flip as §3. Hot, peer-reachable, consensus-adjacent → don't churn for marginal gain. |
| `node.py` `aliascheck` (`WHERE openfield = ?`) | No `openfield` index → full scan, but on mainnet `node.token_index` (the aliases side-index) is the real fix and it is **off pre-fork** (plugin inert). Revisit with the side-index; add `LIMIT 1` opportunistically. |
| `token_index._sum_party` (`token_index.py:138`) | Iterate-all-with-predicate (no early break) → quadratic backfill, but it is the **tokens_aliases plugin**, inert on mainnet pre-fork. Clean bounded-range follow-up (encode the credit `<h` / debit `<=h` boundary). |
| `rest_api` supply `SUM` (`rest_api.py:199`) | Genuine full scan, but runs in a **daemon thread**, single-flight, **cached to disk**, incremental top-up on tip advance — never blocks a request. Leave. |
| `chain_ops.recompress_ledger` | Already a single range scan; the VACUUM runs on a temp copy. Its real issue is a **correctness** blocker, not perf (see §6). |
| `block_store.build_from_sqlite`, `shieldedv1`, `scripts/snapshot.py` | Already correctly batched/incremental/height-keyed — the *model to copy*, not fix. |
| `staking.py`, `reward_chain.extract`, `migrate_amounts.py`, `ledger_explorer.py` | Experimental-off / one-shot offline tools / separate process. Out of scope; just never point them at the live `ledger.db`. |
| `rpc_bitcoin.py:124` signature LIKE | Same fix as §2.2 but behind the default-OFF `rpc_bitcoin` flag (inert on prod); apply opportunistically. |

---

## 5. Post-fork degradation watchlist

These are acceptable today but **grow with the post-fork chain** — track and address before they bite:

- **From-zero rebuilds on every reorg.** `chain_ops` rebuilds `balance_index` and `txid_index` from the
  whole chain to undo even a 1-block reorg. `keep_height` is already in scope at the call site (it is
  passed to `vm_engine.rebuild`) but **not** to the balance/txid rebuilds. Make them rewind to
  `keep_height` (balance_index has `rollback_rows`; txid_index needs a height-keyed secondary), keeping
  the full rebuild as a flag-gated correctness fallback.
- **`txid_index.rollback` is O(all-indexed-txids)** — it scans the whole txid→height map (keyed by random
  txid) to find the few rows above `keep_height`. Add a height-ordered secondary db (`height||txid`, like
  shieldedv1's `kimg_h`/`notes_h`) to make rollback a range-delete.
- **`vm_engine.rebuild` re-executes ALL post-fork `vm:` txs** from fork activation on every reorg (cost
  scales with total VM usage, not reorg depth). Needs periodic **VM-state checkpointing** so a reorg
  replays only from the last checkpoint ≤ `keep_height`.
- **`ledger_balance3` per-block cost** rises with each address's history — the balance-index-primary flip
  (§3) must be activated post-fork or this becomes the dominant per-block consensus cost.
- **`/api/fork`** (`rest_api.py:686`) does a genesis→tip `GROUP BY` + unindexable `openfield LIKE '%hf2%'`,
  cached per tip. Once fork lock-in is permanent, freeze the cached result and skip the scan forever.
- **`node.py` `addlist` / `addlistlim`** materialize+sort a hot address's full sender+recipient history
  per call. Rewrite as the UNION-of-two `(party, block_height)`-index-ordered-`LIMIT` subqueries form
  already used by REST `_address_txs` (`rest_api.py:1309`). Whitelist-gated, so lower urgency.

---

## 6. Correctness blockers surfaced by the audit (NOT perf — must precede `ledger_integer_amounts`)

- **`chain_ops.recompress_ledger`** (`chain_ops.py:140-147`) is **not integer-storage-safe** — it must be
  converted before `ledger_integer_amounts` is set on mainnet.
- **`balances.py` blanket `except: <value> = 0`** silently *zeroes* a balance on any conversion error.
  Narrow these once amounts are integer end-to-end (the perf pass deliberately preserved them verbatim).

---

## 7. Validation

All landed fixes were verified **without touching the prod ledger or spawning long-lived nodes**:
- Hermetic storage parity/rebuild suite (`test_balance_index`, `test_txid_index`, `test_storage_backend`,
  `test_sequencing_conn_leak`): **18/18** — proves the narrowed rebuilds are byte-identical.
- `test_transactions` (balanceget) + `test_api` + `test_rest_api` (signature lookup): **40/40**.
- The audit itself was **source-only** (no `static/ledger.db` / `hyper.db` reads), per the
  no-heavy-scans-on-prod constraint; index-existence claims rest on `dbhandler.py` / `db_migrations.py`
  source, not a `PRAGMA` against prod.
