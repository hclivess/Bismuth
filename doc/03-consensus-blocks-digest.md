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
      truncate fields to max lengths; verify the signature via the fork-gated
      `SignerFactory.verify_tx_signature(post_fork, …)` (`post_fork = block_height >= node.fork_height`,
      the single hf2 fork). **Post-fork ordinary single-sig secp256k1** uses the Ethereum-shape path:
      the signature is a 65-byte recoverable hex sig over the 32-byte **content txid** (the canonical
      pre-image `(timestamp, address, recipient, amount, operation, openfield)`), the `public_key`
      field is **dropped**, the signer is recovered via `ecrecover` and must match the sender address,
      and low-s is enforced. **All other cases** — every pre-fork tx, and post-fork RSA / ED25519 /
      native multisig / shielded — keep the legacy path:
      `verify_bis_signature(sig, pubkey_b64, buffer, address)` over the explicit public key, where
      `buffer = str((timestamp, address, recipient, amount, operation, openfield)).encode()` (multisig
      requires M-of-N DER sigs over that frozen buffer — it does **not** sign the txid). The
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
      diff, …, new_pow=…)` where `new_pow` is true once `height >= node.fork_height` (the single
      hf2 fork — the PoW swap is bundled into it), selecting the modernised blake2b Heavy3
      (see [04](04-pow-and-difficulty.md)).
   10. **Balances & fees** — for every non-coinbase tx: enforce the per-block tx-age window, sum the
       sender's debits+fees across the block, compute the fee, and require
       `balance >= amount` and `balance - block_debit - block_fees >= 0`
       (via `essentials.ledger_balance3`, cached per address). Remove each confirmed signature from
       the mempool.
   11. **Update tip** — set `node.last_block` / `node.last_block_hash` (then write).
   12. **Plugin hooks** — fire `block` / `fullblock`.
   13. **Post-fork reject checks** — once `block_height >= node.fork_height` (the dynamic hf2 fork; all
       three are inert pre-fork and on regnet/when state is absent), still BEFORE the commit:
       - **VM state-root** (`digest.py` ~545): the coinbase MUST commit the pre-state VM root
         (`vm_engine.extract_state_root`); a missing root, or one `!= node.vm_state_root`, raises and
         rejects the block (see [19](19-vm.md)).
       - **Multisig timing** (`digest.py` ~572): a multisig SENDER address
         (`SignerFactory.address_is_multisig`) is only accepted at/after `fork_height`; any multisig
         spend at a lower height raises (chain-split safety — receiving INTO a multisig is always fine).
       - **Shielded value** (`digest.py` ~588): `shieldedv1.validate_block(...)` consensus-validates the
         block's `shield:` txs (key-image double-spend, value conservation, ownership proofs); failure
         raises. Parsed ops are stashed and applied to the LMDB sidecar only after `to_db` succeeds.
   14. **Write** — `db_handler.to_db(block, diff_save, block_transactions)` (one batched commit).
   15. **Mirror hash** — `blake2b(latest-block rows, digest_size=20)`.
   16. **Dev/HN rewards** — only when `height % 10 == 0` and `height < 4,380,000`:
       `db_handler.dev_reward(...)` and `db_handler.hn_reward(...)` (negative "mirror" block heights).
   17. **Token update** — if the block carried `token:issue`/`token:transfer`, run
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

**Mining reward** (`min` floor 0.5 BIS; the switch is keyed on the fixed `POW_FORK`, **not** hf2):

| Range | Formula |
|---|---|
| pre-`POW_FORK` (height < 1,450,000) | `15 - height/500000 - 2.4` |
| post-`POW_FORK` mainnet (≥ 1,450,000) | `15 - (height-1450000)/1100000 - 9.5` |
| post-`POW_FORK` testnet (≥ 894,170) | `15 - (height-894170)/1100000 - 9.5` |

The coinbase `reward` column stores `mining_reward + sum(fees in block)` — all fees go to the miner.

**Fee** (`essentials.fee_calculate`): `base + len(openfield)/100000`, plus **+10** for
`operation == "token:issue"`, plus **+1** when `openfield` starts with `alias=`; quantized to 8 dp.
`base` is the static `BASE_FEE = 0.01` pre-fork (and whenever no `base_fee` is passed). **Post-fork**
the caller passes the **dynamic base fee** (`fee_dynamics.base_fee`): `0.01 × clamp(avg(recent block
weights)/TARGET_WEIGHT, 0.5, 10)`. The congestion signal is per-block **WEIGHT/`TARGET_WEIGHT` (30)**
read from the LMDB block store (`block_store.recent_block_weights`) — NOT a SQLite tx-count. Post-fork
also adds two execution surcharges (only when `vm_surcharge=True`): **+0.01** (`fee_dynamics.VM_SURCHARGE`)
for `operation` starting `vm:`, and **+1** for `operation` starting `shield:` (doc/22 EC-validation cost).

**Dev / HN rewards**: applied every 10th block until height 4,380,000, written as negative-height
"mirror" rows by `db_handler.dev_reward` / `hn_reward`.

## Hardforks (`fork.py`)

There are **two distinct forks**, gated by **different signals** — do not conflate them:

**1. Legacy `POW_FORK` (a FIXED height, `Fork.__init__`)** — the historical 2020 reward/version fork.

| Symbol | Value | Meaning |
|---|---|---|
| `POW_FORK` | 1,450,000 | mainnet legacy reward/version fork height |
| `POW_FORK_TESTNET` | 894,170 | testnet equivalent |
| `FORK_AHEAD` | 5 | blocks before the fork at which old protocol versions start being rejected |
| `REWARD_MAX` | 6 | reward at `POW_FORK+1` ≥ this ⇒ wrong chain ⇒ rollback (`check_postfork_reward`) |
| `versions_remove` | `mainnet0017..0020` | versions banned after the fork |

At `POW_FORK`: the reward formula switches, old versions are dropped (`limit_version`), the tx-age
window tightens to 2 h, and the checkpoint granularity drops from 1000 to 30 blocks (≈ max 59-block
rollback). This is a **fixed-height** fork — nothing here reads `node.fork_height`.

**2. Dynamic `hf2` fork (`node.fork_height`, signal-activated)** — the modern single bundle. Its height
is **not** a constant: it is derived deterministically from an on-chain miner signal
(`FORK2_SIGNAL = "hf2"` in coinbase openfields; `dynamic_fork_height` locks in after a
`FORK2_WINDOW`-block run and activates a `FORK2_BURY`-buried round boundary later) and persisted to a
sidecar. `node.fork_height` is `None` until lock-in, so every rule below is inert on mainnet pre-fork.
Everything bundled into hf2 gates on `block_height >= node.fork_height`: the blake2b Heavy3 PoW swap
(`new_pow`, pipeline step 9), the fork-aware signature path (steps 3, `SignerFactory.verify_tx_signature`),
the three post-fork reject checks (step 13: VM state-root, multisig timing, shielded `validate_block`),
and the LWMA difficulty retarget (`difficulty_lwma.py`, gated in `difficulty.py`). There is one signal
and one activation height — never add a second fork knob.

**Regnet** never participates in either fork's difficulty path: `difficulty(node, …)` returns a fixed
`regnet.REGNET_DIFF` tuple immediately when `node.is_regnet` (`difficulty.py:75`), so the LWMA/legacy
controllers are bypassed entirely.

## Ledger schema (created by `genesis.py`)

`transactions(block_height INTEGER, timestamp, address, recipient, amount, signature, public_key,
block_hash, fee, reward, operation, openfield)` — 12 columns. Amounts/timestamps are stored as text.
Negative `block_height` rows are dev/HN "mirror" reward rows; `address='Hyperblock'` rows are
balance-consolidation rows produced by hyperblock pruning. A `misc(block_height, difficulty)` table
(created by the DB layer) stores per-block difficulty. See [05](05-database-and-ledger.md).
