# 07 — Mempool

`mempool.py` is the unconfirmed-transaction staging area. The process holds one `Mempool` instance,
`mp.MEMPOOL`, created at startup with the node-wide `db_lock`.

## Storage

```sql
transactions(timestamp, address, recipient, amount, signature, public_key, operation, openfield,
             mergedts INTEGER(4) DEFAULT (strftime('%s','now')))   -- 9 cols
```

`mergedts` is the local receive time (not broadcast to peers). RAM mode
(`mempool_ram`, default true; forced on for regnet) uses `file:mempool?mode=memory&cache=shared`
with WAL; otherwise the on-disk `mempool.db`. `check()` recreates the file if the column count isn't 9.

## Acceptance rules (`merge()`)

`merge(data, peer_ip, c, size_bypass=False, wait=False, revert=False)` is the single entry point
(`c` is the ledger cursor). Pre-flight: empty/`'*'` handling, peer-freeze check, sequence-type check
(bad format freezes the peer 10 min), and a `db_lock` check (drop or wait per `wait`; skipped when
`revert=True` because the rollback caller already holds `db_lock`).

Per transaction (inside `self.lock`):
1. size gate (`space_left_for_tx`, unless `size_bypass`);
2. parse timestamp / amount; validate sender & recipient addresses; enforce field max-lengths;
3. mandatory-message check (exchanges in `config.mandatory_message` require a non-trivial openfield);
4. amount ≥ 0, not future-dated, not older than `REFUSE_OLDER_THAN = 7200 s` (2 h);
5. signature verifies (`SignerFactory.verify_bis_signature`, same buffer as consensus);
6. not already in mempool (`sig_check`) nor in the ledger;
7. balance: `amount <= ledger_balance_before_mempool` **and** `balance - fee >= 0`
   (fee via `essentials.fee_calculate`);
8. insert (with `mergedts = now`), commit, append the literal string `"Success"` (callers match on it
   — do not change it).

> A fix applied during the revival: an invalid-timestamp transaction now `continue`s instead of
> falling through with a stale value (see [14](14-known-issues-and-improvements.md)).

## Size tiers (`space_left_for_tx`, MB of valid txs)

| mempool size | admitted |
|---|---|
| < 0.3 | everything |
| 0.3–0.4 | `token:` operations or openfield > 200 chars |
| 0.4–0.5 | amount > 5 BIS |
| 0.5–0.6 | senders in `config.mempool_allowed` |
| ≥ 0.6 | nothing |

`size_bypass=True` skips the gate (used for local wallet submissions via `mpinsert` and for rollback
re-insertion).

## Interaction with consensus

`digest_block()` waits for `mempool.lock` to be free, then deletes each confirmed signature via
`delete_transaction()`. On block rollback, the node re-inserts the rolled-back transactions with
`merge(..., revert=True)`. Peer exchange is rate-limited: `sendable()` (≥30 s since last send),
`tx_to_send()` (new txs since the peer's last sync, minus signatures the peer already has), `sent()`.

## Locks

`self.lock` guards reads-with-`write=True`, writes, and the whole `merge()`. `self.peers_lock`
guards the `peers_sent` freeze dict. The external `db_lock` is only *checked* here, never acquired.
`purge()` deletes txs older than 2 h.
