# 03 — Consensus: blocks, digestion & rewards

The consensus core is `digest.py`. `digest_block(node, data, sdef, peer_ip, db_handler)` accepts a
list of blocks (each a list of raw transaction tuples) and validates+commits them atomically.
`fork.py` holds hardfork heights; `genesis.py` bootstraps the chain. This document is the
authoritative reference for validation rules — do not change behavior here without the
characterization tests (see [14](14-known-issues-and-improvements.md)) passing identically.

## Digestion pipeline (per block)

1. **Ban check** — if `node.peers.is_banned(peer_ip)`, raise immediately.
2. **Lock** — acquire `node.db_lock` (skip the whole digest if already held); spin until
   `mp.MEMPOOL.lock` is free.
3. For each block (`process_block_data` → `BlockProcessor`):
   1. **Stop check** — bail if `node.IS_STOPPING`.
   2. **Fork-reward guard** — past `fork.POW_FORK` (mainnet 1,450,000 / testnet 894,170), verify the
      reward stored at `POW_FORK+1` is `< REWARD_MAX (6)`; otherwise the node is on the old chain →
      `rollback_under(POW_FORK-1)` and abort.
   3. **Parse & sign-check every transaction** (`from_raw_transaction` + `Transaction.validate`):
      truncate fields to max lengths; verify the signature with
      `SignerFactory.verify_bis_signature(sig, pubkey_b64, buffer, address)` where
      `buffer = str((timestamp, address, recipient, amount, operation, openfield)).encode()`. The
      **last** transaction in the block is the coinbase/mining tx (amount 0, RSA sender, nonce =
      first 128 chars of its openfield).
   4. **Block timestamp ordering** — coinbase timestamp must be strictly greater than
      `node.last_block_timestamp`.
   5. **Duplicate-signature check** — no signature may already exist in the on-disk or RAM ledger,
      and there are no intra-block duplicates.
   6. **Difficulty** — `difficulty(node, db_handler)` returns the 8-tuple; stored in `node.difficulty`.
   7. **Block hash** — `sha224(str(transaction_list_converted) + last_block_hash)`.
   8. **Duplicate-block check** — reject if `block_hash` already exists.
   9. **Proof-of-Work** — `mining_heavy3.check_block(height, miner_address, nonce, last_block_hash,
      diff, …)` (see [04](04-pow-and-difficulty.md)).
   10. **Balances & fees** — for every non-coinbase tx: enforce the per-block tx-age window, sum the
       sender's debits+fees across the block, compute the fee, and require
       `balance >= amount` and `balance - block_debit - block_fees >= 0`
       (via `essentials.ledger_balance3`, cached per address). Remove each confirmed signature from
       the mempool.
   11. **Update tip** — set `node.last_block` / `node.last_block_hash` (then write).
   12. **Plugin hooks** — fire `block` / `fullblock`.
   13. **Write** — `db_handler.to_db(block, diff_save, block_transactions)` (one batched commit).
   14. **Mirror hash** — `blake2b(latest-block rows, digest_size=20)`.
   15. **Dev/HN rewards** — only when `height % 10 == 0` and `height < 4,380,000`:
       `db_handler.dev_reward(...)` and `db_handler.hn_reward(...)` (negative "mirror" block heights).
   16. **Token update** — if the block carried `token:issue`/`token:transfer`, run
       `tokens.tokens_update()`.
4. **Checkpoint** — `checkpoint_set(node)` advances `node.checkpoint` (the rollback floor).
5. **Cleanup (`finally`)** — `db_handler.db_to_drive()` flushes; release `db_lock`; fire `digestblock`.

On any exception, the chain tip is restored from the database, the peer earns a warning (and is
banned past threshold), and `ValueError("Chain: digestion aborted")` propagates.

## Transaction validation rules

Field maxima (truncation in `from_raw_transaction`): address/recipient 56, signature 684, public
key (b64) 1068, operation 30, openfield 100,000; nonce 128; timestamp `%.2f`; amount `%.8f`.

A transaction is rejected unless all hold:
- timestamp not in the future, and within 24 h of the last block timestamp;
- amount ≥ 0; sender and recipient pass `address_validate`;
- signature verifies and is non-empty and unique (ledger + intra-block);
- per-block tx-age window: ≤ 2 h before the block timestamp from block 1,450,000 onward (was 24 h);
- sender can afford `amount` and the block's cumulative debits + fees.

Coinbase tx additionally: must be last, amount exactly 0, RSA sender address.

No `operation` whitelist exists — any ≤30-char string is allowed. `token:issue`/`token:transfer`
and `openfield` starting with `alias=` trigger feature processing and fee surcharges.

## Reward & fee model

**Mining reward** (`min` floor 0.5 BIS):

| Range | Formula |
|---|---|
| pre-fork (height < 1,450,000) | `15 - height/500000 - 2.4` |
| post-fork mainnet (≥ 1,450,000) | `15 - (height-1450000)/1100000 - 9.5` |
| post-fork testnet (≥ 894,170) | `15 - (height-894170)/1100000 - 9.5` |

The coinbase `reward` column stores `mining_reward + sum(fees in block)` — all fees go to the miner.

**Fee** (`essentials.fee_calculate`): `0.01 + len(openfield)/100000`, plus **+10** for
`operation == "token:issue"`, plus **+1** when `openfield` starts with `alias=`; quantized to 8 dp.

**Dev / HN rewards**: applied every 10th block until height 4,380,000, written as negative-height
"mirror" rows by `db_handler.dev_reward` / `hn_reward`.

## Hardforks (`fork.py`)

| Symbol | Value | Meaning |
|---|---|---|
| `POW_FORK` | 1,450,000 | mainnet PoW fork height |
| `POW_FORK_TESTNET` | 894,170 | testnet equivalent |
| `FORK_AHEAD` | 5 | blocks before the fork at which old protocol versions start being rejected |
| `REWARD_MAX` | 6 | reward at `POW_FORK+1` ≥ this ⇒ wrong chain ⇒ rollback |
| `versions_remove` | `mainnet0017..0020` | versions banned after the fork |

At the fork: reward formula switches, old versions are dropped (`limit_version`), the tx-age window
tightens to 2 h, and the checkpoint granularity drops from 1000 to 30 blocks (≈ max 59-block
rollback).

## Ledger schema (created by `genesis.py`)

`transactions(block_height INTEGER, timestamp, address, recipient, amount, signature, public_key,
block_hash, fee, reward, operation, openfield)` — 12 columns. Amounts/timestamps are stored as text.
Negative `block_height` rows are dev/HN "mirror" reward rows; `address='Hyperblock'` rows are
balance-consolidation rows produced by hyperblock pruning. A `misc(block_height, difficulty)` table
(created by the DB layer) stores per-block difficulty. See [05](05-database-and-ledger.md).
