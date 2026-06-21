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
5. signature verifies via the fork-aware `SignerFactory.verify_tx_signature`, bound to the
   **destination** block height (next block). Post-fork an ordinary single-sig secp256k1 tx is
   recovered from its recoverable sig over the content txid (no pubkey, signer via `ecrecover`,
   low-s enforced); pre-fork txs and post-fork RSA / ED25519 / native-multisig keep the legacy
   buffer + explicit-pubkey check (multisig: N-of-M over the frozen buffer, never the txid). This is
   only a pre-filter — the digester re-verifies and is authoritative;
6. not already in mempool (`sig_check`) nor in the ledger;
7. balance: `amount <= ledger_balance_before_mempool` **and** `balance - fee >= 0`
   (fee via `essentials.fee_calculate(..., block=last_block)`; see fee note below);
8. insert (with `mergedts = now`), commit, append the literal string `"Success"` (callers match on it
   — do not change it).

> A fix applied during the revival: an invalid-timestamp transaction now `continue`s instead of
> falling through with a stale value (see [14](14-known-issues-and-improvements.md)).

## Fees (`essentials.fee_calculate`, `fee_dynamics.py`)

`fee = base + len(openfield)/100000`, plus +1 BIS for an `alias=` openfield and +10 BIS for a
`token:issue` operation, quantized to 8 dp. `base` is the static `BASE_FEE` pre-fork. Post-fork
(`hf2`) the base becomes the **dynamic base
fee** from `fee_dynamics.base_fee(BASE_FEE, recent_block_weights)`: a smooth, calculable, EIP-1559-style
analogue of the LWMA difficulty. The congestion signal is per-block **WEIGHT**, not tx count — each
block's load is `tx count + openfield_bytes // W_UNIT` (`W_UNIT=1000`), a gas/vbyte-style measure, so a
block of large RingCT/VM txs prices in its real footprint, not merely how many txs it holds. `base_fee`
scales the base over a `WINDOW=20`-block demand window toward `TARGET_WEIGHT=30`, clamped to `[0.5×, 10×]`
— it is deterministic and stateless (a pure function of recent block weights, no saved fee state across
restarts). The weight window is read from the post-fork **block store (LMDB)**
(`block_store.recent_block_weights`, computed once per block in `digest.py` ~494), never SQLite — the old
SQLite `recent_tx_counts` helper was removed. `fee_calculate` also takes a `vm_surcharge` flag that adds
`VM_SURCHARGE = 0.01` (gas) to `vm:` operations and a flat +1 BIS to `shield:` operations post-fork. The
mempool's own fee checks call `fee_calculate` with the static base and no surcharge today; the dynamic
value and surcharges are gated on the fork (set per block as `node.base_fee` in `digest.py`).

## Size tiers (`space_left_for_tx`, MB of valid txs)

Anti-spam admission (the doc/17 rewrite): as the pool fills, the gate tightens to higher-**fee** txs.
The priority is the tx's actual deterministic fee (`essentials.fee_calculate(openfield, operation)`) —
the one thing a spammer cannot inflate without paying it — **not** the nominal `amount` (a self-send
inflates that for free) and **not** a per-address allow-list (addresses are free to mint, so it was
Sybil-trivial). The earlier `amount > 5 BIS` / `config.mempool_allowed` tiers are gone.

| mempool size | admitted |
|---|---|
| < 0.3 | everything |
| 0.3–0.4 | fee > `BASE_FEE` (data / token / alias txs — anything above the bare base fee) |
| 0.4–0.5 | fee ≥ 1 BIS (alias-register / token-issue economic tier and up) |
| 0.5–0.6 | fee ≥ 10 BIS (token-issue tier — the highest deterministic fee) |
| ≥ 0.6 | nothing |

`size_bypass=True` skips the gate (used for local wallet submissions via `mpinsert` and for rollback
re-insertion). The hard protections — every tx pays a fee out of a *funded* balance, and total pool
size is bounded — live in `merge()`; this routine only orders admission under congestion.

## Interaction with consensus

`digest_block()` waits for `mempool.lock` to be free, then deletes each confirmed signature via
`delete_transaction()`. On block rollback, the node re-inserts the rolled-back transactions with
`merge(..., revert=True)`. Peer exchange is rate-limited: `sendable()` (≥30 s since last send),
`tx_to_send()` (new txs since the peer's last sync, minus signatures the peer already has), `sent()`.
(These query helpers and the SQL constants now live in `mempool_queries.py` / `mempool_sql.py`.)

The **built-in solo miner** (`miner.py`, opt-in `mine=True`) is the other consumer: it pulls pending
txs with `mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND)` (`SELECT * … ORDER BY amount DESC`, capped at
`MAX_TX_PER_BLOCK`), appends its signed hf2 coinbase, and digests the assembled block — the same
mempool feed the regnet `regtest_mine` command and the regnet `generate_one_block` use. See
[21](21-mining.md).

## Locks

`self.lock` guards reads-with-`write=True`, writes, and the whole `merge()`. `self.peers_lock`
guards the `peers_sent` freeze dict. The external `db_lock` is only *checked* here, never acquired.
`purge()` deletes txs older than 2 h.
