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
| `hyperlane_asyncio.py` | class-level `asyncio.get_event_loop()` raises on Python ≥3.10 | create a loop in `__init__` |
| `mining.py` | used `Decimal` without importing it | added `from decimal import Decimal` |
| `node.py` | `addlistlimmirjson` sent its response twice | removed the stray second `send()` |
| `aliases.py` | private duplicate of `replace_regex` (uncached) | reuse `essentials.replace_regex` |

### Consensus: fork-stall mitigation (the 59-block checkpoint)
`blocknf()` rolls back one block at a time and refuses once the tip drops below `node.checkpoint`
(`node.py`), where `checkpoint = round_down(last_block, 30) - 30` (≈30–59 blocks below the tip). The
checkpoint is **not** recomputed mid-rollback, so the maximum rollback is ~59 blocks. A node that
diverges onto a minority/alternate fork by more than ~59 blocks — e.g. after a network partition of
~1 h at 60 s/block — can therefore never roll back far enough to rejoin the longest chain and
**stalls on the wrong fork**. This matches the periodic fork-stall reports.

The checkpoint is a deliberate anti-deep-reorg guard, so it must not simply be removed. Changes made:
- **Diagnosability** — the checkpoint-skip now logs at **WARNING** (it was `INFO`, invisible at the
  default level) with actionable guidance, so a stalled node says *why* it won't roll back.
- **Configurable depth** — `rollback_depth` (`config.txt`, default **30** = unchanged) sets the
  post-fork checkpoint distance. A stuck operator can raise it so the node rolls back deeper and
  rejoins the longest chain. This trades some deep-reorg resistance for liveness — an informed,
  local policy choice; it changes no validation rule and does **not** hard-fork the network. Gated by
  `tests/test_characterization.py::test_checkpoint_set_depth_default_and_configurable`.

Recommended follow-up (needs design, not done here): make the tolerance **consensus-aware** — allow a
deeper rollback only when a supermajority of peers agree on a strictly longer valid-PoW chain — to
restore liveness without weakening deep-reorg resistance. Also investigate the *root cause* of the
>59-block divergences: partition handling and the `consensus_most_common` vs `consensus_max`
selection in `worker.py`.

### Tooling
- A **dependency-light pytest suite** replaces the `bismuthclient`/`bismuthcore`-based tests (which
  don't run on 3.12). See [12](12-tooling-build-tests.md). `.travis.yml` now targets Python 3.12.

## Known, not yet fixed

- **`received_block_height` possibly-undefined** in the `blockheight` sync handler (`node.py:764`,
  `:783`). `pyflakes` flags it; it appears to be assigned on the live path before use, but the control
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
- `python3 -m pytest -v` — the full suite (42 tests) against a managed regnet node.
- `python3 -m pyflakes node.py` — undefined-name guard after import changes.
- The AST arity check used during the revival confirmed every cross-module call site matches its
  definition (no signature drift).
