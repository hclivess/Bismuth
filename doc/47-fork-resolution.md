# Fork resolution: measured evidence → possession → one rollback

**Status: IMPLEMENTED (2026-08-18), default ON (`fork_resolution=measured`), opt-out `fork_resolution=legacy`.**
Code: `fork_resolution.py` (decision logic + transport), `chain_ops.blocknf` (orchestration),
callers in `node.py` (inbound `blocknf`/`blocknfhb`) and `worker.py` (outbound). Tests:
`tests/test_fork_resolution.py` (28, mutation-checked). No wire-protocol change: only pre-existing
commands are used, so a node running this talks to every deployed legacy node.

Ported from nado's consensus consolidation of 2026-08-17/18 (nado `doc/finality.md` §3, commits
`72e064af`…`39794b5a`: *"rollbacks require measured evidence, not one donor's word — or its silence"*,
*"ties resolve once at the first divergent block, and no rollback without possession"*), adapted to
Bismuth's PoW **longest-valid-chain** rule (nado is PoS and decides by a peer/stake majority; a PoW node
must adopt any strictly longer valid chain regardless of headcount — so the majority probe is used only
for the deep-rollback gate, and *possession + full validation* carries the rest).

---

## 1. What was wrong

The legacy reorg was one peer's word, acted on blind, one block at a time:

```
peer (height == pool max) says blocknf(<our tip hash>)     # "I don't have your tip"
  -> roll back ONE block, immediately, before holding any competing block
  -> send "sync", peer says blocknf again for the new tip -> roll back another …
```

* **Disruption was free.** Anyone able to advertise the pool-maximum height could make a node shed real
  blocks (bounded only by `rollback_depth`), then serve nothing. Every rolled block re-entered the
  mempool and had to be re-fetched from someone else.
* **A same-height fork made both sides roll back.** Each side is the other's "not found" and both are at
  the pool max, so both rolled, both re-fetched, and which branch survived depended on socket timing —
  the seesaw nado watched for hours.
* **Round-trip-per-block rollbacks** turned any fork deeper than a few blocks into a rollback storm, and
  every rollback fired a flat 2-point "Rollback" strike at the peer that triggered it — an *honest* peer
  serving a *legitimate* longer chain got banned after ~15 reorgs, isolating the node (a fork driver).
* **Silence was read as disagreement** in places (`if not client_block:` → `blocknf`), and a hyperblock
  peer's trimmed history looked like a fork.

## 2. The rules now (nado's, PoW-adapted)

| # | Rule | Where |
|---|---|---|
| 1 | **Absence of information is never evidence of divergence.** Every probe is tri-state: `True` (peer serves our hash at that height) / `False` (it *answered* and doesn't) / `None` (unreachable, timeout, refused, malformed). `None` never rolls back — it ends the measurement as `UNKNOWN`. | `PeerLink.knows`, `find_common_ancestor` |
| 2 | **The rollback is bounded by a measured ancestor.** Before touching the ledger: find the highest height at which the advertiser still knows *our* hash — a linear walk of 6 for the common shallow case, then binary search (≤ ~16 probes for the 720-block window). Rolling past a proven ancestor is pure loss. | `find_common_ancestor`, `measure` |
| 3 | **Possession before rollback.** Fetch the competing branch from the ancestor forward through the ordinary `blockheight`/`blocksfnd` serve path (batches, capped at 1000 blocks / 90 s), require it to reach **strictly higher** than our tip, and only then roll back to the ancestor **in one operation** and apply the held branch through the one canonical apply path (`digest.process_block_data`: PoW, signatures, balances, hash linkage, fork gating). Rollback + apply happen **under one `db_lock` hold**, so no concurrent digest can build on the bare ancestor. If the branch fails validation, our backed-up rows are re-applied, the peer is penalised (reputation + 5-point strike) and the verdict is cached. An attacker must now present a *held, longer branch that survives full validation* to cause any revert. | `chain_ops._blocknf_measured`, `fetch_branch`, `_apply_blocks_locked` |
| 4 | **Ties resolve once, at the first divergent block.** Same advertised height ⇒ compare the two branches' block hashes at `ancestor+1` (`blockgetjson`), a value that never changes as the branches grow; the lexicographically lower wins, both sides compute the same answer, exactly one side reorgs — after possession. Missing evidence ⇒ "ours" (no evidence never switches a node off its chain). | `tie_winner` |
| 5 | **Deep rollbacks need corroboration.** An ancestor below the node's rollback checkpoint goes through the doc/14 gate (`rollback_allowed`: 75 % height consensus, ≥3 peers, ≥1 reputable) **and** a hash-level majority: a strict majority of the *answering* peers (advertiser + up to 8 others, each a fresh tri-state probe) must not know our tip. Too few answers ⇒ refused. `DEAD_FORK` is logged loudly with the manual remedies. | `deep_ok`, `majority_disagrees` |
| 6 | **Verdict first, donor second.** If the advertiser's listening port can't be dialled (inbound-only / NAT), the branch is measured and fetched from an outbound peer at ≥ its height that *also* doesn't know our tip. | `candidate_peers` |
| 7 | **No strike for an honest reorg.** The flat "Rollback" strike is legacy-only. Measured mode strikes only when a claim was *disproved* (served nothing above the ancestor, branch shorter than advertised, branch failed validation). | callers in `node.py` / `worker.py` |
| 8 | **Verdicts are cached** per `(our tip hash, peer)` for 60 s and dropped whenever our tip changes, so a nagging peer costs one measurement per minute, and a stale `REORG` can never revert a chain we just adopted. `fork_lock` serialises measurements across peer threads; nothing is measured while a digest holds `db_lock`. | `_cache_*`, `invalidate` |

Everything else stays: `rollback_depth`/checkpoint semantics, `rollback_consensus*`, hyperblock skips,
the `rollback` plugin hook, mempool re-injection of rolled-back txs, `handle_processing_error`'s
ledger/working heal.

## 3. What was deliberately NOT ported

* **Production suppression on a measured minority fork** (nado `a073fc49`). In PoW the only correct
  policy is to mine on the longest *valid* chain the node holds; if a longer chain exists the node adopts
  it under rule 3, and if it cannot be obtained/validated, pausing our own hashpower helps nobody.
* **FFG / two-floor finality, stake-signed verdicts, watchtower slashing** — PoS machinery with no PoW
  analogue. Bismuth's floors remain the checkpoint (`rollback_depth`) and the doc/14 deep gate.
* **Mempool reject cooldowns** — a fork driver only under deterministic block production.

## 4. Operating

* `config.txt`: `fork_resolution=measured` (default) | `legacy` (the old blind one-block rollback).
* Log lines start with `Fork resolution (<peer>)`: the verdict (`behind/synced/reorg/dead_fork/unknown/
  tie_win`), probes used, ancestor, what was held, what was applied.
* Tunables in `fork_resolution.py`: `PROBE_TIMEOUT_S 5`, `LINEAR_PROBES 6`, `MAX_PROBE_DEPTH 720`,
  `FETCH_CAP_BLOCKS 1000`, `FETCH_BUDGET_S 90`, `VERDICT_TTL_S 60`, `MIN_ANSWERS_DEEP 2`,
  `MAX_PROBE_PEERS 8`.
* Probes use `block_height_from_hash` / `blockgetjson`, which peers serve to `allowed` IPs (`any` by
  default). A peer that refuses them simply yields `UNKNOWN` — no rollback, and the ordinary sync from
  other peers still carries its blocks.

## 5. Live validation: the two-node regnet harness

Regnet nodes never dialled and dropped `hello`, so no socket-level reorg could be observed. Two env
switches (harness-only, off by default) lift that: `BISMUTH_REGNET_PEERING=1` makes a regnet node dial
its peer file and answer `hello`; `BISMUTH_REGNET_PEERS_SEED="ip:port,…"` seeds that file at boot (or the
test writes it later — the client loop reloads it every 5 s while isolated).

`tests/test_two_node_fork_resolution.py` (`BISMUTH_RUN_TWONODE=1`) runs two node processes on
4070-4073 with isolated data dirs, mines A=6 / B=3 from the shared genesis **unpeered**, then peers them:

* **longer chain wins, once** — B measured `reorg — proven divergence above 1 [4 probes, ancestor 1]`,
  held A's 6 blocks first, then `rolling back 3 block(s) to the measured ancestor 1` and
  `adopted the peer's branch — tip 4 -> 7`; exactly one rollback line, A never rolled back;
* **same-height race** — both mined one block concurrently while connected; a genuine tie was produced
  and A logged `same-height fork from 7: THEIR branch wins the tie-break at 8 (ours d4424a1b… vs theirs
  7b764245…)`, rolled once, adopted; B never moved. Both runs converged in < 20 s total, no seesaw.

Also observed: concurrent measurements from the two directions (inbound + outbound) were serialised by
`fork_lock` (`another fork resolution is in progress`), and the second saw the tip already gone.
