# 30 — From-genesis sync & historical validation exceptions

## The problem

A fresh node should be able to sync the whole chain **from block 1 to the tip**,
re-verifying every block as it goes. In practice a from-genesis *verifying*
replay halts: a handful of historical **mainnet** blocks cannot pass today's
validation rules because they were produced by **manual ledger interventions**
at the time they happened —

* **coin rescues** — lost-key recoveries / stuck-fund moves inserted directly
  into the ledger (an unsigned or re-signed transaction, or a balance edit that
  reads as an overspend on replay), and
* **hard-fork-edge fixes** — one-off adjustments around a fork boundary
  (a reward edit, an out-of-order or hand-inserted block).

The network already agreed on these blocks years ago and buried them under
millions of proof-of-work blocks. But a strict replay re-checks the signature,
the balance, the duplicate-signature rule and the PoW of *every* block, so the
first irregular block raises a `ValueError` and the new node can never reach the
tip from genesis.

> The *rule transitions* themselves replay fine — the legacy code already
> height-gates the historical reward schedule, timestamp drift, checkpoint
> depth, PoW algorithm (`POW_FORK = 1450000`), difficulty controller and the
> dev/HN reward cutoff (height `4380000`). What needs handling is the small set
> of blocks that broke a rule **by hand**.

## The mechanism — two complementary, opt-in pieces

Both live in [`validation_exceptions.py`](../validation_exceptions.py) and are
**mainnet-only**. They are **inert for any live node**: a synced or
snapshot-bootstrapped node never re-digests historical heights, so neither the
checkpoint check nor the trusted-prefix skip ever changes its behaviour. They
take effect *only* during a genuine from-genesis catch-up. (Setting
`assume_valid_height = 0` disables the trusted prefix entirely and re-validates
every block from genesis; the checkpoint hashes are still verified.)

### 1. `assume_valid_height` + checkpoints — a trusted, hash-anchored prefix

For blocks at or below the trust horizon `assume_valid_height`, skip the per-item
validation — **signature, block-timestamp ordering, proof-of-work,
duplicate-signature and overspend**. Integrity is not taken on faith: it is
guaranteed *in aggregate* by **checkpoints**. Because a Bismuth block hash chains
the previous block hash, the canonical block hash at height H recursively commits
the **entire** chain 1..H. A from-genesis node still recomputes every block's hash
and, on reaching a checkpoint height, must reproduce the hardcoded canonical
value — any tampering anywhere in the skipped prefix changes the chained hash and
halts the sync. This is the "a single CRC at height ~4M replaces a thousand
signature validations" idea, realised with the (stronger) recursive block-hash
chain.

This is what makes a real from-genesis replay both **possible** (the earliest
history can't pass today's stricter signature/timestamp rules — see *Findings*)
and **practical** (no millions of memory-hard PoW + RSA re-verifications).

* Config: `assume_valid_height` (or env `BISMUTH_ASSUME_VALID_HEIGHT`).
* Default `0` ⇒ **off** ⇒ every check runs on every block, exactly as before.
* Recommended: set it to a checkpoint height (e.g. `4000000`) so the trusted
  prefix is anchored at its top edge.
* Checkpoints (`MAINNET_CHECKPOINTS`) are **verified always** — independent of
  `assume_valid_height`. This is pure added safety: inert for a synced node
  (which never re-digests historical heights), it fires only as a catch-up passes
  a checkpoint height. A mismatch halts (the prefix is not the canonical chain).
* Wired in `digest.py` (timestamp / duplicate / PoW / overspend skips +
  `verify_checkpoint` after the block hash is computed) and
  `digest_tx.Transaction.validate(verify_signature=...)`.

### Starting a sync from block 1 (`sync_from_genesis`)

The horizon/checkpoint above removes the *validation* halts, but a fresh mainnet
node still **bootstraps from a snapshot by default** (that's where the genesis
block lives). To actually begin from block 1:

* Config `sync_from_genesis` (or env `BISMUTH_SYNC_FROM_GENESIS=1`).
* On a fresh ledger this **seeds the canonical genesis block** (`chain_ops.seed_genesis`
  — height 1, block hash `7a0f3848…`, the `4edadac…` genesis address, reused from
  `regnet.SQL_LEDGER`) and **skips the snapshot download** (`bootstrap()` is the single
  choke point; it seeds instead of fetching). Catch-up then builds block 2..tip from
  peers, accepting the trusted prefix below the horizon and anchoring it at each
  checkpoint.
* It never clobbers a ledger that already has blocks, and is a no-op once synced —
  so it is safe to leave on.
* Regnet starts from genesis natively (it always seeds `SQL_LEDGER`), which is what
  the end-to-end test below exercises.

### 2. The exception registry — targeted per-height waivers

For the specific manual-intervention blocks that fail a **structural** check
(overspend / duplicate / pow / timestamp), or a signature even above the
assume-valid height. Each entry names:

```python
height: {"checks": {OVERSPEND, ...},      # which checks to waive at this height
         "reason": "coin rescue …",        # logged whenever the waiver fires
         "signatures": {"<sig-prefix>"}}    # SIGNATURE check only: pin to exact tx(s); None = whole block
```

* **Targeted.** Only the named checks at the named height are waived; the
  signature waiver can be pinned to specific transaction signatures. Everything
  else is validated in full.
* **Historical-only.** Every key is a fixed past height — nothing here can
  affect a newly mined tip block.
* **Loud.** Every applied waiver logs `VALIDATION EXCEPTION at height H …`.
* **Fail-closed.** A malformed registry never crashes consensus — it validates.

The checks map one-to-one to the raise sites in `digest.py`:

| check | raise site | what a waiver allows |
|---|---|---|
| `signature` | `sort_and_validate_transactions` → `Transaction.validate` | a tx whose signature (or field) check fails |
| `overspend` | `BlockProcessor._validate_balance` | a sender spending more than it owns / fee shortfall |
| `duplicate` | `BlockProcessor.check_duplicate_signatures` | a replayed signature (cross-block or in-block) |
| `pow` | `BlockProcessor.verify_proof_of_work` | a manually-inserted block with no/irregular PoW |
| `timestamp` | block-timestamp ordering check | an out-of-order historical block |

### Where the list lives

The curated set is `MAINNET_EXCEPTIONS` in `validation_exceptions.py` (empty
until the heights are confirmed). It can also be supplied as **data** without a
code change via an external JSON file:

* Config `validation_exceptions_file` (or env `BISMUTH_VALIDATION_EXCEPTIONS_FILE`).
* JSON shape: `{ "<height>": {"checks": [...], "reason": "...", "signatures": [...]|null}, ... }`.
* On mainnet the file is **merged over** the in-source set; the discovery tool
  emits exactly this shape.

## Discovering the heights

> **Never scan the live `static/ledger.db`** — it is the running node's 23 GB
> production ledger and a full scan I/O-starves it. Work on a copy:
> `sqlite3 static/ledger.db ".backup /tmp/ledger_copy.db"` (consistent while the
> node runs), or point the tools at a peer.

Two methods, complementary:

1. **Batch scan (cheap classes).**
   [`tools/find_validation_exceptions.py`](../tools/find_validation_exceptions.py)
   walks a ledger **copy** and emits a candidate JSON registry for the
   state-free anomaly classes it can find exactly — duplicate signatures
   (cross-block + in-block) and, with `--check-signatures`, signature-verification
   failures. It **refuses** to open the live prod ledger.

   ```
   sqlite3 static/ledger.db ".backup /tmp/ledger_copy.db"
   python3 tools/find_validation_exceptions.py --ledger /tmp/ledger_copy.db \
       --check-signatures --out exceptions.json
   ```

2. **Iterative real-sync (the structural classes).** Overspend / PoW / timestamp
   need the full balance/PoW replay, so the reliable way to surface them is to
   let the real consensus code find them in order: start a node from genesis,
   let it **halt** on the first `ValueError`, add that `(height, check)` to the
   registry, restart, repeat until it reaches the tip. Each halt is logged with
   the exact height and reason, so curating the list is mechanical.

Always **review** the emitted list before trusting it — every entry *loosens* a
historical block.

## Findings — the mainnet ledger (scan of a snapshot to height 4,845,489)

A full scan of a mainnet ledger copy (the bismuth.cz bootstrap snapshot) was run
through the detectors above. The result is the reason the design centres on the
checkpoint, not a big registry: **every from-genesis blocker is in the early
chain, below the 4,000,000 trust horizon.**

* **Overspend — none.** Exactly two addresses have a negative net balance, and
  both are the synthetic *placeholder senders* on the mirror-reward rows
  (`"Development Reward"`, `"Hypernode Payouts"`), not real spendable addresses.
  No real transaction ever overspends; a faithful replay never trips it.
* **Duplicate — one.** Height **708335** re-includes 10 transactions already in
  708334 (a reorg artifact at the ~700k hard fork). Below the horizon → covered
  by the checkpoint.
* **Timestamp — early only.** ~870 blocks have a coinbase timestamp ≤ the
  previous block's, **all below height ~60,000** (2017 sub-0.01s block spacing).
  Below the horizon → covered.
* **Signature — early only, systematic.** Tens of thousands of early-chain
  transactions fail re-verification under today's stricter rules — early
  1024-bit-RSA signing-buffer / address-binding drift, not per-incident rescues.
  Targeted samples across 1M / 2M / 3M / 4M / 4.8M and a focused re-verification
  of the **(4,000,000 … tip]** range found **zero** failures. All are below the
  horizon → covered.

So the populated artifact is the **checkpoint set** (`MAINNET_CHECKPOINTS`,
computed and re-verified from the copy), not a list of per-height waivers — the
4M checkpoint anchors the entire early prefix at once. `MAINNET_EXCEPTIONS` stays
**empty**; the registry remains available for any *future* discrete anomaly that
appears **above** the horizon (where full validation still runs).

## Safety properties

* **Checkpoint-anchored.** The trusted prefix is only skipped when a checkpoint
  commits it (no checkpoints, or a horizon past the last checkpoint ⇒ no skip).
  A from-genesis node recomputes every block hash and halts on any checkpoint
  mismatch, so a forged early prefix cannot be accepted.
* **Inert for live nodes.** A synced (or snapshot-bootstrapped) node never
  re-digests historical heights, so neither the prefix skip nor the checkpoint
  check changes its behaviour. The horizon only affects a genuine from-genesis
  catch-up.
* **Network-scoped.** Mainnet only; regnet/testnet have no checkpoints, so they
  never enter the trusted prefix even with a horizon configured (tests keep full
  validation). Per-node overrides (`node.checkpoints`, `node.validation_exceptions`)
  exist for tests.
* **Monotonic loosening.** A registry entry can only *accept* one specific
  historical block that would otherwise be rejected; it cannot make the node
  reject anything it accepts today, and cannot affect new blocks.

## Tests

[`tests/test_validation_exceptions.py`](../tests/test_validation_exceptions.py)
— 21 hermetic tests: the registry logic (height/check/signature scoping,
assume-valid threshold, external-file load/merge, fail-closed, inert-by-default);
the checkpoint logic (match / mismatch-halts / not-a-checkpoint, mainnet set
present, anchor-required so an unanchored or no-checkpoint horizon never skips);
and the real `BlockProcessor` wiring (overspend / duplicate / signature each
suppressed only when registered, the trusted prefix skipping overspend, and
`assume_valid` skipping the signature verify entirely).

[`tests/test_sync_from_genesis.py`](../tests/test_sync_from_genesis.py) — genesis
seeding (canonical genesis, no-clobber of a populated ledger, the `bootstrap()`
guard seeding instead of downloading) plus an **end-to-end regnet two-node test**
(env-gated `BISMUTH_RUN_TWONODE`): node A mines a chain past a checkpoint; node B
starts at genesis and catches A's chain over REST with the checkpoint +
`assume_valid_height` set — it reaches A's tip and logs `checkpoint OK`, while a
third node given a WRONG checkpoint **halts at the checkpoint height** with
`checkpoint MISMATCH`. (Verified: passes in ~73s.)
