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
**mainnet-only** and **inert by default** (an empty registry + `assume_valid`
off is byte-identical to having no mechanism at all).

### 1. `assume_valid_height` — a trusted checkpoint (like Bitcoin's `-assumevalid`)

For blocks at or below this height, skip the **expensive per-transaction
signature re-verification** only. The block is still bound to the real chain by
proof-of-work, the block-hash linkage, the difficulty retarget, the timestamp
ordering **and** the overspend/duplicate checks — none of those are skipped.
This is purely a speed optimisation for deeply-buried history.

* Config: `assume_valid_height` (or env `BISMUTH_ASSUME_VALID_HEIGHT`).
* Default `0` ⇒ **off** ⇒ every signature is verified, exactly as before.
* Wired in `digest_tx.Transaction.validate(verify_signature=...)`, driven from
  `BlockProcessor.sort_and_validate_transactions`.

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

## Safety properties

* **Mainnet-scoped.** Testnet/regnet return an empty registry (overridable
  per-node via `node.validation_exceptions` for tests).
* **Default-inert.** Empty registry + `assume_valid_height = 0` ⇒ no behavioural
  change; the node validates exactly as before.
* **Monotonic loosening.** Adding an entry can only ever *accept* one specific
  historical block that would otherwise be rejected; it cannot make the node
  reject anything it accepts today, and cannot affect new blocks.

## Tests

[`tests/test_validation_exceptions.py`](../tests/test_validation_exceptions.py)
— 17 hermetic tests: the registry logic (height/check/signature scoping,
assume-valid threshold, external-file load/merge, fail-closed, inert-by-default)
plus the real `BlockProcessor` wiring (overspend / duplicate / signature each
suppressed **only** when registered, and `assume_valid` skipping the signature
verify entirely).
