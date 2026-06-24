# doc/35 — Difficulty-divergence detector + guarded self-heal (#23, Stage 2a)

Prevents a recurrence of the difficulty-corruption incident (node mined at 84.48 while the network was at
~89.8). Root cause (mapped, `difficulty.py`): the controller is **recursive on the STORED previous value** —
`difficulty.py:70` reads the cached difficulty from the `misc` table into `diff_block_previous`, and `:94`
returns `diff_block_previous + adjustment`. Consensus-valid block-times only steer the *delta* around that
base, so a wrong cached value at/below the tip is a **self-reinforcing fixed point**. "Recompute-and-assert"
can't catch it (`difficulty()` reuses the same corrupt `misc` row — the same call `verify_proof_of_work` uses
at `digest.py:650`). The only independent reference is **a peer that derived difficulty from a clean `misc`
history**. Hence: detect by peer-quorum comparison; heal by rolling back below the corruption + resyncing so
`difficulty()` re-derives from a clean base. Related but distinct from [[mainnet-bringup]]'s manual fix and
doc/31's accuser (malicious rollback); this is *accidental* self-corruption.

## Detector (observe-only)
A `_difficulty_divergence_loop()` daemon thread in `node.py` (modeled on `_autoheal_loop`, its own
`DbHandler`, `interval=300s`), each cycle:
1. Skip unless ready: `node.difficulty` set, not initial-syncing (`last_block ≈ peers.consensus`), and not
   within ±`DIFFICULTY_WINDOW` (1440) blocks of `node.fork_height` (LWMA transition can legitimately diverge).
2. Resolve each connected socket peer's REST port via `/api/capabilities` (`rest_client.get_capabilities`,
   cached ip→rest_port; skip non-REST peers — "if the API is inaccessible it doesn't exist").
3. Poll up to 8 peers' `/api/difficulty` (new `rest_client.get_difficulty`, fail-soft like `get_height`).
   Keep only **height-matched** samples (peer `block_height == node.last_block`, ±1).
4. Need ≥ `min_peers` (3) height-matched samples, else **abstain** (never heal on thin data). Compute the peer
   **median** (robust to one liar/fork). `DIVERGED` iff `abs(local − median) > threshold` (0.5 diff-units OR
   2% relative, whichever larger) **and** ≥75% of samples agree among themselves within `threshold/2`.
5. **Debounce:** require `N_confirm`=3 consecutive DIVERGED cycles at the same-or-higher local tip with a
   stable median; any CLEAN/abstain resets the counter. The detector reads only the cached `node.difficulty`
   + an HTTP poll — **never scans the ledger** (cf. [[no-heavy-scans-on-prod-ledger]]).

Core logic factored as `chain_ops.detect_difficulty_divergence(node, db, peers, threshold) -> (diverged,
local, median, sample_count)` (unit-testable, beside `autoheal_live`).

## Action posture — SAFE BY DEFAULT
Three independent config flags; the prod-safe default is **detect + log only**:
- `diff_divergence_detect` (default **True**): run the detector; on confirmed divergence emit a LOUD warning
  (local vs median vs sample_count) + a plugin alert hook. **No other action.** Cheap (a 5-min HTTP poll).
- `diff_divergence_pause_mining` (default **False**, opt-in): on confirmed divergence set `node.mining_paused`
  (checked in `miner.py`/`mining.py` before using `difficulty()`), since a wrong local difficulty in *either*
  direction makes our mined blocks orphan-bound. Auto-clears on the next CLEAN reading (itself debounced).
- `diff_divergence_autoheal` (default **False**, opt-in — enable only after regnet/testnet soak): perform the
  guarded heal below.

## Guarded self-heal (opt-in)
Reuse the proven one-shot `rollback_to` trigger (`node.py:1512-1519`, runs at the one safe startup point that
already rebuilds derived state), **not** an in-place deep rollback:
1. target = `max(last_block − rollback_depth(30), checkpoint)`, clamped to never go below `node.checkpoint` /
   the MAINNET_CHECKPOINTS finality floor; gated by `essentials.rollback_allowed()` (supermajority +
   reputable-peer, anti-sybil).
2. Write `target` to the `rollback_to` file under `db_lock`; request a clean restart (systemd auto-restarts).
   On boot, the existing trigger consumes the file once and `rollback()→resync` re-derives a clean `misc`.

**Hard loop guards** (per-ledger sidecar JSON next to the ledger — `<ledger>.diffheal.json`, NOT in the db,
so regnet/mainnet never share heal state):
- **Cooldown** ≥1h between heals (one full resync+stabilize window).
- **Lifetime cap** a PERMANENT per-ledger cap of 2 on the **monotonic** `heal_count` — the hard stop. (A
  rolling-24h-window limit *alone* re-opens every day, so a corruption that survives every resync would
  restart-loop the node forever at 2/day — caught in review.) Once `heal_count` hits the cap the node **stays
  advisory-only and NEVER restarts again**, emitting a persistent "manual intervention required" warning. The
  budget resets to 0 **only on a confirmed-CLEAN post-heal reading** (a genuinely fixed corruption restores
  the budget for a future unrelated incident; one that survives resync never reads clean, so it stays capped
  forever). A secondary ≤2-per-rolling-24h rate limit also applies. This is the exact restart-loop
  [[debug-root-cause-not-symptoms]] warns against.
- **Bounded depth**: never below checkpoint; successive heals may go one `rollback_depth` deeper, capped at
  `3×rollback_depth`, then advisory-only.
- **Once-per-boot**: an in-memory flag blocks a second arm before the restart completes.

## Risks guarded
Transient peer disagreement (debounce + height-match + supermajority + ≥3 peers); network-wide wrong consensus
(median+supermajority best-effort, `rollback_allowed` reputable gate, advisory default leaves the call to a
human); restart-loop (cooldown+max-heals+bounded-depth); reconcile/hyper wedge (heal only writes the file +
restarts — the proven startup sequence does the truncation, never an in-place deep rollback from the thread);
REST-port mismatch (capabilities discovery); prod I/O (no ledger scan); HF2/LWMA transition (skip window).

## Regnet soak (validated — `tools/diff_selfheal_soak.py`)
A 3-node regnet cluster brought to a common tip via `api_sync` (A mines; B,C catch up over REST), then the
real `detect_difficulty_divergence` is driven against the live 3-peer quorum across phases. Result (PASS):
**HEALTHY** 50 cycles, real ≥3 quorum, **0 false positives**, 0 exceptions; **INJECT_LOCAL** divergence
detected every cycle; **LYING_PEER** a single peer reporting a wrong difficulty (via the prod-inert
`BISMUTH_TEST_DIFF_OFFSET` regnet hook) does **not** flag us — robust.

**Soak finding (fixed):** `connection_pool` holds SOCKET addresses, and `/api/capabilities` isn't served on
the socket port — so probing only the socket port resolved **zero** peers and the detector would have
**perpetually abstained on a real network** (the unit/two-node tests masked this by feeding REST ports
directly). `_resolve_rest_port` now probes the standard convention **REST = socket+1** (mainnet 5658→5659),
then the socket port, then our own REST port, and trusts the authoritative `rest_port` from the capabilities
body. Covered by `test_rest_port_resolved_via_socket_plus_one_convention`.

## Test plan (regnet only — never prod ledger / 5658-5659 / systemd)
Two-node regnet harness (model `tests/test_two_node_api_sync.py`). Regnet `difficulty()` is the constant
`REGNET_DIFF`, so **inject** the corruption (set node B's `node.difficulty` + its `misc` tip row wrong).
Assert: detector resolves A's rest_port, height-matches, returns `diverged=True`; **one** diverged cycle does
**not** heal (debounce); after `N_confirm` the `rollback_to` file is written with the clamped target; restart
→ B rolls back+resyncs to A and B's difficulty == A's; heal happens **exactly once** (next reading CLEAN, no
further file); with a persistent re-injected corruption, after `max_heals` it **stops + goes advisory + does
not restart again** (proves no loop); cooldown suppresses a second heal in-window; advisory mode writes no
`rollback_to`, logs the warning, sets+clears the mining-pause flag.
