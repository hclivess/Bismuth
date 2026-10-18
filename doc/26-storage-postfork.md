# doc/26 — Storage: the LMDB-only ledger (migration complete)

> Status: **✅ complete.** The ledger is one LMDB environment. Every relational database the node used to
> carry — the full ledger, the pruned hyperblock copy, the RAM working copy, the token/alias index file
> and the mempool file — is gone, along with all the machinery that kept them in step. The authoritative
> description of the store is [05](05-database-and-ledger.md); this page records what the migration
> retired and where everything lives now.

## Where things live

| data | home |
|---|---|
| blocks, block hashes, deduped pubkeys | `blockstore-<ledger>`: `blocks`, `hashes`, `pk`/`pkr` |
| per-block difficulty | same env: `diff` |
| signature / address-history indexes | same env: `sig`, `addr` |
| balances (integer units) + fixed allocations | same env: `bal`, `meta` "alloc" |
| post-fork content txids, pubkey registry | same env: `txid`, `pkref` |
| tokens + aliases | `tokenindex-<ledger>` (the `tokens_aliases` plugin, [27](27-plugins.md)) |
| mempool | process memory ([07](07-mempool.md)) |

One block commits — body, difficulty, every index, the balance deltas — in **one LMDB write transaction**
(`BlockStore.commit_block`); a reorg is one transaction (`BlockStore.rollback`). LMDB supplies the
atomicity, MVCC readers for the REST API and ordered keys for range scans; there is nothing left to
reconcile on restart.

## What was retired

- **The multi-file ledger.** Separate full / pruned / working copies written by separate connections, and
  the crash-recovery contract (commit markers, cross-file attach, startup reconcile) that existed only
  because there were several files to keep in step.
- **Hyperblock pruning.** History is never collapsed into synthetic balance rows; the LMDB store is
  compact enough to keep the full chain (mainnet ~9.7 GB, pubkey dedup: 5,586 distinct keys across
  6.4M txs).
- **RAM mode** and its periodic flush to disk.
- **Negative-height mirror reward rows.** The dev / hypernode rewards minted every 10th block until
  4,380,000 are fixed balance allocations (`ledger_genesis.MAINNET_ALLOCATIONS`, `meta` "alloc").
- **Side stores and shadow indexes.** The separate balance, txid and pubkey-registry envs, the read/write
  seam with its parity-assert modes, the difficulty sidecar — all folded into the one env and maintained
  in the block commit.
- **Tarball bootstrap.** A fresh node takes a DB-direct LMDB snapshot from peers ([43](43-p2p-snapshot.md))
  or seeds genesis and syncs.
- **Options:** `hyper_path`, `hyper_recompress`, `full_ledger`, `ram`, `mempool_ram`, `mempool_path`,
  `trace_db_calls`, `autoheal`, `autoheal_interval`, `bootstrap_url`, `bootstrap_file`,
  `snapshot_path`, `block_store`, `balance_index`, `balance_index_consensus`, `txid_index_consensus`,
  `pk_registry_consensus`, `parity_strict`, `token_index`, `rebuild_db`.

## Upgrading an older store

A block store written before the ledger became LMDB-only holds blocks but not the difficulty record or
the indexes. The node detects the old schema (`meta` "schema" ≠ `block_store.SCHEMA`) and refuses to
start, naming the command. With the node stopped:

```
python3 scripts/upgrade_ledger.py static/blockstore-ledger.db --anchor http://185.100.232.5:5659
```

It replays the difficulty history with `difficulty.retarget`, anchored on the exact values peers publish
at `/api/stats/difficulty`, then rebuilds every index with `BlockStore.reindex` and stamps the schema.

## Consensus boundary

Storage sits behind the frozen serialization boundary (`bismuth_serialize`): block hashes and signing
pre-images are computed from the 12-field row, not from the stored bytes, so the at-rest format
(including the post-fork true-bytes `txrec` packing of [40](40-storage-truebytes.md)) is free to change
without touching consensus. `replay_verify.py` recomputes every block hash from the store to prove it.
