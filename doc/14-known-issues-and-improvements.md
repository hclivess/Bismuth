# 14 — Known issues, fixes applied & improvement roadmap

This document records the bugs/cleanups verified during the revival, what was **fixed**, and a vetted
roadmap of further improvements that were intentionally **deferred** (to keep consensus behavior
identical under the available test coverage).

## Fixed in this revival

### Reintegration & wiring
- **polysign vendored in-tree** (`polysign/`). It was an external pip dependency *and* gitignored, so
  it never shipped with the repo; legacy versions also fail to build on Python 3.12. The non-RSA
  signers (coincurve/ed25519) are now **lazy-loaded**, so an RSA-only mainnet node depends only on
  `pycryptodomex`. RSA signing/verification behavior is unchanged (verified by the test suite).
- **`from digest import *` removed from `node.py`.** The wildcard silently re-exported digest.py's own
  imports; because digest.py imports `regnet` only *locally*, `regnet` (and `os`) were undefined at
  regnet startup — the node crashed with `NameError`. Imports are now explicit. (`pyflakes` is clean
  apart from the pre-existing item below.)

### Bug fixes
| File | Bug | Fix |
|---|---|---|
| `plugins.py` | `unload_plugin(name)` recursed into itself (masked by a bare `except`) | call `_unload_plugin(name)` |
| `dbhandler.py` | `tokens_user` used `WHERE address OR recipient = ?` (a boolean expression) | `WHERE address = ? OR recipient = ?` |
| `mempool.py` | invalid-timestamp tx fell through without `continue` (reused a stale value) | added `continue` |
| `staking.py` | `execute_param` referenced an undefined module-global `app_log` | guarded optional parameter |
| `send_csv.py` | module-level `print(sys.argv[3])` crashed on too few args; literal `None` args | use `--wallet`, pass empty operation/openfield |
| `attic/hyperlane_asyncio.py` | class-level `asyncio.get_event_loop()` raises on Python ≥3.10 | retired to `attic/` (unused, broken stub) |
| `mining.py` | used `Decimal` without importing it | added `from decimal import Decimal` |
| `node.py` | `addlistlimmirjson` sent its response twice | removed the stray second `send()` |
| `aliases.py` | private duplicate of `replace_regex` (uncached) | reuse `essentials.replace_regex` |

### Consensus: fork-stall mitigation (the 59-block checkpoint)
`blocknf()` rolls back one block at a time and refuses once the tip drops below `node.checkpoint`
(`chain_ops.py` ~209; `checkpoint_set`/`round_down` live in `essentials.py`), where
`checkpoint = round_down(last_block, 30) - 30` (≈30–59 blocks below the tip). The
checkpoint is **not** recomputed mid-rollback, so the maximum rollback is ~59 blocks. A node that
diverges onto a minority/alternate fork by more than ~59 blocks — e.g. after a network partition of
~1 h at 60 s/block — can therefore never roll back far enough to rejoin the longest chain and
**stalls on the wrong fork**. This matches the periodic fork-stall reports.

The checkpoint is a deliberate anti-deep-reorg guard, so it must not simply be removed. Changes made:
- **Diagnosability** — the checkpoint-skip now logs at **WARNING** (it was `INFO`, invisible at the
  default level) with actionable guidance, so a stalled node says *why* it won't roll back.
- **Configurable depth** — `rollback_depth` (`config.txt`, default **30** = unchanged) sets the
  post-fork checkpoint distance. Gated by
  `tests/test_characterization.py::test_checkpoint_set_depth_default_and_configurable`.
- **Reputation-gated AUTO-RECOVERY deep rollback (default ON)** — `essentials.rollback_allowed(node,
  target_height)` governs the `blocknf()` guard. Shallow rollbacks (≥ checkpoint) are always allowed.
  A rollback *below* the checkpoint no longer strands the node needing a manual re-bootstrap: with
  `rollback_consensus=True` (**now the default**) the node rolls back as deep as needed to rejoin the
  chain proven peers agree on, allowed only when a supermajority (`rollback_consensus_threshold`, 75%)
  of enough peers (`rollback_consensus_min_peers`, 3) agree **AND** the agreeing set includes
  `rollback_consensus_min_reputable` (default 1) **proven** peers — positive reputation in
  `peers_reputation` = have delivered valid PoW blocks. So a fresh sybil flood cannot force a deep reorg
  (it has no proven blocks), a single/minority peer cannot either, and the deep chain's blocks are
  PoW-validated on ingest regardless — an attacker still needs real 51% work. This replaces the rigid
  `rollback_depth` stranding (the operational pain) with self-healing. Gated by
  `tests/test_rollback_autorecover.py` + `tests/test_characterization.py::test_rollback_allowed_policy`.

Remaining root-cause work (not done here): investigate *why* >59-block divergences form in the first
place — partition handling and the `consensus_most_common` vs `consensus_max` selection in
`worker.py`. The Sybil-resistance of the consensus signal also bounds how strong the supermajority
gate really is, and should be hardened (e.g. weight by distinct peers / known good versions).

### Tooling
- A **dependency-light pytest suite** replaces the `bismuthclient`/`bismuthcore`-based tests (which
  don't run on 3.12). See [12](12-tooling-build-tests.md). `.travis.yml` now targets Python 3.12.

## Known, not yet fixed

- **A restart costs a full-chain fork-signal scan (~14 min on mainnet), and the tip looks frozen while it
  runs.** `digest.process_block_data` runs `fork.dynamic_fork_height` **once per process**
  (`_first_scan = not node._fork_caught_up`), forward-scanning **every height 1..tip** — ~4.9M individual
  `SELECT openfield ... WHERE block_height=?` — before flipping to the cheap incremental `lockin_at_tip`.
  It holds the digest path, so the tip cannot advance until it finishes. The result is only persisted by
  `save_locked_height` once hf2 actually **locks in**; hf2 has not, so nothing is written and **every** boot
  redoes the whole scan.

  **It is not a wedge — do NOT restart into it**, that throws the work away and starts over. Confirm health:
  1. `py-spy dump --pid <pid>` shows a worker *active* in `fork.py` / `dynamic_fork_height`;
  2. `rchar` in `/proc/<pid>/io` keeps climbing (~20 MB/s) — it is reading, not blocked;
  3. the journal is silent. NB `Chain:` lines are **not** WARNING-level — a healthy 9.7-day run logged zero
     of them, so "no `Chain:` lines" proves nothing;
  4. the block hash at the local tip **matches a live peer** at the same height. Use
     `GET /api/block/height/<h>` and read `transactions[0].block_hash` — there is **no top-level
     `block_hash`** key, so a naive `.get('block_hash')` returns `None` on *both* sides and looks like a
     false match.

  Fix worth doing: persist a "scanned to height H, no lock-in yet" watermark so a restart resumes instead of
  rescanning from 1.

- **`received_block_height` possibly-undefined** in the `blockheight` sync handler (`node.py`).
  `pyflakes` flags it; it appears to be assigned on the live path before use, but the control
  flow should be made explicit.
- **No payload size cap on the Linux `receive()` path** (`connections.py`). The non-Linux path caps at
  100 MB; the Linux `poll` path does not, so a malicious peer could request a huge allocation. A
  symmetric cap should be added.
- **SQL built by string interpolation** in `apihandler.api_gettransaction_for_recipients` (address
  list), and `api_getblocksafterwhere` is hard-disabled (`raise ValueError("Unsafe…")`). Both should
  be reworked with parameterized queries before exposure.
- **`txsend` / `keygen` transmit private keys** over the socket — localhost-only, and `txsend` is
  deprecated; candidates for removal.
- **Dead code**: `mining.py` (legacy PoW, unused); `digest.block_already_exists` can never return
  `True` (its caller's `continue` is unreachable); a wrong return annotation on
  `Transaction.from_raw_transaction`.

## Improvement roadmap (behavior-preserving, do behind tests)

> Update: items 1 and 3 (difficulty) are now **implemented** and verified end-to-end — the shared
> `db_helpers.retry_db` backs every module's retry loop, and difficulty.py's controller constants are
> named. Both are gated by `tests/test_db_helpers.py` and `tests/test_characterization.py` (the latter
> pins `difficulty()` outputs on a synthetic ledger so the renaming is provably identical). The
> remaining items stand.

These were scoped out deliberately: the regnet test suite covers the happy path well but not every
consensus branch, so deeper consensus refactors should land with characterization tests for each
touched function first.

1. **Shared DB retry helper.** `dbhandler`, `mempool`, `essentials.execute_param_c`, `ledger_queries`
   and `staking` each carry a near-duplicate execute/commit retry loop with subtly different
   abort/retry policies. Consolidate into one helper parameterized by `(retry_forever | abort_on)` so
   the exact per-call-site semantics are preserved. (Biggest readability win.)
2. **Balance consolidation.** `node.balanceget` (the source TODO already says "move to DbHandler"),
   `essentials.ledger_balance3`, `apihandler._get_balance` and `ledger_queries` quick-balance compute
   balances in parallel; route them through one implementation.
3. **Name consensus magic numbers** in `difficulty.py` / `mining_heavy3.py` / `fork.py`
   (target 60 s, `Kd=10`, divisor 720, window 1440, floor 50, drop 180/360, checkpoint 30/1000,
   `heavy3a` size & sentinels) into named constants — values unchanged.
4. **Difficulty edge cases** (documented, not changed): the per-block adjustment is clamped only on the
   upside; `ceil(28 - diff/16)` goes non-positive at `diff ≥ 448` (far above current values).
5. **Modernize remaining `from X import *`-style fragility and legacy `Cryptodome` direct use** in
   `essentials.sign_rsa` / `keys_check` (the source TODOs point at finishing the polysign migration).
6. Optionally vendor `bismuthcore`/`bismuthclient` equivalents or provide a thin in-repo client, so the
   external client libraries are no longer needed by any tooling.

## How changes are verified

- `tests/regnet_smoke.py` — chain advance, rewards, signatures, fee formula (no pytest needed).
- `python3 -m pytest -v` — the full suite against a managed regnet node.
- `python3 -m pyflakes node.py` — undefined-name guard after import changes.
- The AST arity check used during the revival confirmed every cross-module call site matches its
  definition (no signature drift).
