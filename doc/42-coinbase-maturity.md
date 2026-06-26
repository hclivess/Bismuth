# doc/42 — coinbase maturity (block rewards unspendable until buried)

**Status:** IMPLEMENTED, gated on `node.fork_height` (folds into the single hf2 fork — no new signal).

## Goal

A coinbase **reward** is not spendable until it is `COINBASE_MATURITY` blocks deep. BTC-style maturity,
adapted to Bismuth's **account** model (there is no coinbase *output* to lock — the miner's address
balance just rises by the reward, so maturity is a **height-aware spendable-balance carve-out**).

## Why

If a miner spends a freshly-credited reward and that block is later reorged out, every downstream tx
that received those coins is cascade-invalidated — rugging honest recipients. Maturity makes the reward
unspendable until it is buried beyond any plausible reorg, so a reorg can never unwind an already-spent
reward.

## The constant — deliberately NOT `rollback_depth`

```
COINBASE_MATURITY = 100   # essentials.py — FIXED hf2 consensus constant
```

It is **not** tied to `node.rollback_depth` (default 30), and that decision is load-bearing:

- `rollback_depth` is a **per-node `config.txt` value** (`options.py`); a *consensus* spend rule cannot
  depend on a value each operator picks.
- 30 is a **soft** bound, not finality: `rollback_consensus` (on by default) permits deeper,
  reputation-gated rollbacks to rejoin a longer chain — those can unwind coins matured at 30.
- Empirically reorgs here are rare/shallow (0 rollbacks in the live node's history), so 30 covers
  *normal* operation — but maturity exists for the *worst* case.

`100` ≈ 1.7h at ~1-min blocks: familiar (BTC), comfortably clear of the 30 normal bound and of realistic
deep-recovery rollbacks, with only modest pool-payout latency. Tune ONLY before hf2 activation.

## Mechanism (account-model carve-out)

```
immature_coinbase(addr, V) = Σ reward  WHERE recipient = addr AND reward != 0
                                        AND block_height > V − COINBASE_MATURITY
spendable(addr, V)         = ledger_balance(addr) − immature_coinbase(addr, V)
```

`V` = the height the spend is validated into (`node.last_block + 1`). A reward credited at block `B` is
mature iff `V − B ≥ COINBASE_MATURITY`. Only the *recent* reward slice is gated — a miner with a prior
balance still spends that freely; immature coins are a floor the address cannot dip below. The query is
bounded to the recent window via the `block_height` index — never a full-history scan.

## Enforcement (fork-gated; pre-fork byte-identical)

- **`digest.py` (authoritative, consensus):** the overspend check uses `spendable = balance_pre −
  immature` post-fork instead of `balance_pre`. A block whose tx dips into an immature reward is
  REJECTED.
- **`mempool.py` (early reject, optimization):** the same carve-out, so an immature-spend never sits in
  the mempool waiting to fail at digest. Uses the digester-mirrored `self.fork_height`.
- Pre-fork (or `fork_height is None`): `immature = 0`, so `spendable = balance` — behavior unchanged.
- The accounting **balance** is untouched; only *spendability* is gated (maturity ≠ confiscation — the
  reward becomes spendable automatically once it ages past `COINBASE_MATURITY`).

## Interactions

- Composes with the checkpoint finality bound and the doc/31 equivocation-slashing into one
  reorg-defense stack.
- Orthogonal to doc/41 (coinbase field layout) — that changes *where* the reward/nonce live; this
  changes *when* the reward is spendable.
- Pool impact: a pool cannot distribute a block's reward until it matures (~1.7h). Standard; the value
  is pegged to the reorg margin precisely to keep this latency minimal.
