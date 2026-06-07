# 18 — The `hf2` hard fork (automatic, signal-activated)

> Companion to [`16-database-rework-plan.md`](16-database-rework-plan.md) and the roadmap
> [`17-roadmap.md`](17-roadmap.md). This collects everything that **changes consensus** into one
> deliberately-scheduled event, and separates it from the work that does not.

## The single most important distinction

Two kinds of modernization, and they must not be confused:

- **Mining-invariant (no fork, ship per-node, any time).** Storage (LMDB block store + pubkey dedup,
  balance index, reward *shadow*), transport/API, and **a GPU miner for *today's* Heavy3**. None of
  these change the block hash; old and new nodes produce and accept the identical chain. Proven by
  `test_replay` staying byte-identical with them active.
- **The `hf2` fork (changes the block hash / validation → coordinated).** Everything below. A node that
  doesn't upgrade forks off at the activation height. That is the point, and the reason it's gated.

## Activation — deterministic, automatic (✅ framework built)

`fork.py` carries the scheduler (`dynamic_fork_height`, `tests/test_dynamic_fork.py`):

1. Upgraded miners stamp `FORK2_SIGNAL` ("hf2") into their **coinbase openfield** (free-form block
   data — no rule change to *start* signalling).
2. Every node counts the signal from the **same chain**, so the activation height is computed
   identically everywhere — **deterministic, no off-chain peer survey, no split risk**.
3. When the trailing window (`FORK2_WINDOW`, 1000) is **all-signalled**, the fork **locks in**; it
   activates at the next round-1000 boundary past a burial margin (`FORK2_BURY`, ≥ rollback depth).
4. Your off-chain survey (the ~15-node network is small enough to eyeball) is the *confidence gate*;
   the actual decision is the chain's.

Remaining framework: the coinbase-signal **writer** (upgraded miners set it), a `/api/fork` readiness
view, and the `block_height >= fork_height` **gate scaffold** in `digest` — each optimization below
slots behind that one gate, one at a time, replay-validated against pre-fork blocks (unchanged).

## What `hf2` bundles

### A. Serialization & storage (the `# HARDFORK (doc/16)` sites) — design ready
Sign/hash **native integer units + a binary/struct tx encoding**, a bounded **content-hash txid**
(`blake2b(tx_content)`, replacing the `signature[:56]` slice), and **canonical sig/pubkey encoding**
(public key by reference — 1:1 with the address — and raw bytes, not base64). This is the bulk of a
block body. *Risk: low-moderate* — it's a representation change, fully replay-checkable: every pre-fork
block must still re-hash identically, every post-fork block round-trips through the new codec.

### B. Reward-sidechain cutover — foundation built (`reward_chain.py`)
Mint dev/hypernode rewards into the sidechain instead of negative-height mirror rows; balances read
ledger + sidechain. *Risk: moderate* — it's balance-preserving (proven for every address on regnet),
but it touches the consensus balance path, so it's replay- and invariant-gated.

### C. Difficulty stepping → **LWMA** — recommended design
Today's `difficulty.py` is a PID controller (60 s target) estimating hashrate, with an **asymmetric
per-block cap** (`MAX_DIFF_ADJUST = 1.0` up only) plus a separate emergency drop. It's convoluted and
swing-prone — and a small chain is exactly where that hurts (a pool hopping on/off swings difficulty
hard, stranding the chain at high difficulty when it leaves).

Replace it with **LWMA** (Zawy's Linear Weighted Moving Average), the de-facto standard retarget for
small PoW chains:
- Targets the same fixed block time; averages solvetimes over a short window (~60–90 blocks) weighting
  the most recent blocks highest → **fast, symmetric** response to hashrate changes.
- **Bounded** solvetime clamps resist timestamp manipulation; no oscillation, no hand-tuned PID gains.
- Adapted to emit Bismuth's bit-prefix difficulty domain.
- ✅ **Implemented** (`difficulty_lwma.py`, `tests/test_difficulty_lwma.py`): symmetric response (slow
  blocks lower difficulty by the same law fast blocks raise it — directly fixing the up-only ratchet),
  bounded per-retarget steps, single-timestamp-spike resistance, and convergence to the target block
  time under a feedback simulation — all unit-proven. Fork-gated, inert until activation.
*Risk: moderate* — consensus-critical but well-understood and widely battle-tested; deterministic and
unit-testable against recorded solvetime series before activation.

### D. Heavy3 improvement — **optional, highest-risk; recommend caution**
Heavy3 (`sha224` → 1 GB memory-hard anneal → substring-prefix difficulty) is already GPU-mineable
(`gpuminer/` proves it), so **a GPU miner does NOT require changing Heavy3.** If we do change it:
- **Keep the memory-hardness** (the 1 GB junction file) — that's what limits per-GPU advantage and
  resists ASICs; dropping it on a low-hashrate chain invites a single farm to 51% it.
- Worth modernizing: the hash (`sha224` → `blake2b`), and the unusual *substring* difficulty metric
  (→ a clean threshold/leading-bits comparison that's easier to analyze).
- **This is the single most security-sensitive change.** Any PoW change has a transition window where
  hashrate is in flux; on ~15 nodes that window is dangerous.

## Continuity — what happens to the existing chain

The chain is **one continuous chain**; the fork is a boundary, not a restart. Nodes do **not**
reconstruct history.

- **Blocks below `fork_height` stay byte-for-byte, with their original hashes.** They were signed and
  hashed under the old rules, and each block commits to the previous block's hash — re-encoding even one
  would change every subsequent hash and snap the chain. History is immutable by construction.
- **At `fork_height` the new rules apply going forward only.** The first new-format block still
  references the last old-format block's hash, so the two halves join seamlessly. No new genesis, no
  re-sync, no balance reset — state carries straight across.
- **Validation is height-gated:** a node runs the old codec/rules for `height < fork_height` and the new
  ones at/above. Both rulesets live in every upgraded node. `replay_verify` re-hashes the whole chain
  exactly this way (old below, new above), which is how we prove no history is corrupted.
- **Storage is orthogonal to consensus.** A node *may* re-store the old blocks locally in the new
  scalable format (LMDB, pubkey-dedup, integer units) — that changes **no** block hash (it's behind the
  frozen boundary), so the storage wins apply to history too without touching the chain's identity. The
  consensus *serialization* of old blocks is never rewritten; only their local *representation* is.
- **Identifiers** follow the same rule: pre-fork txs keep their `signature[:56]` txids and base64
  sig/pubkey; post-fork txs use the content-hash txid and canonical encoding. Tools resolve both,
  height-gated.

So: nodes simply **continue with new-format data from the fork height**, on top of an unchanged past.

## Honest risk assessment & recommended sequencing

Bundling A+B+C+D into one fork is a lot of consensus surface for a ~15-node chain. Suggested order,
lowest-risk first:

1. **Now, no fork:** finish the storage cutover (read path) and ship the **GPU miner for current
   Heavy3** (`gpuminer/`). Immediate value, zero consensus risk, grows the miner base *before* any fork.
2. **`hf2` fork #1 (A + B + C):** serialization/txid/sig-pubkey + reward cutover + **LWMA** difficulty.
   All replay- or series-testable; no PoW-transition hashrate risk. This is the high-value, well-bounded
   fork.
3. **Later, separately (D):** a Heavy3 change, *only* if wanted, once the network/hashrate is healthier
   — its own signalled fork, with the `gpuminer/` kernels updated in lockstep and the memory-hardness
   kept.

Doing **D in its own fork** keeps the riskiest change isolated and easy to reason about, and means the
GPU miner you ship now keeps working right up to that point. The automatic-activation framework is the
same vehicle for each fork, so nothing is wasted by splitting them.

## The GPU miner is coupled to the PoW

`gpuminer/` implements *today's* Heavy3 exactly. If/when **D** lands, `bis.cu` / `bismuth.cl` must be
updated to the new algorithm and re-validated on real hardware (this repo's CI has no GPU). The miner
and `mining_heavy3.py` must always compute the identical function — otherwise the miner emits invalid
blocks. See [`../gpuminer/README.md`](../gpuminer/README.md).
