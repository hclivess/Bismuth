# Changelog — 2026-06 session (commit-anchored)

Engineering changelog for the work landed this session on `hclivess` main. Each entry is
**what / why / verification**, anchored to real commit hashes (`git log --oneline`). Honest
about shipped-vs-deferred: the SQLite→LMDB *consensus reads* and the lockstep SQLite write
path remain deliberately on SQLite (doc/26 defers them); the storage work below is the
engine-seam stage, not the SQLite retirement.

---

## 1. Decentralized multiplayer Hold'em dApp (doc/28, 32, 33, 34)

A no-trusted-dealer, on-chain-refereed multiplayer poker app: an RV32I VM contract is the
funds/showdown referee, the deal is mental poker (no dealer), and all keys live in the
browser. Built stage-by-stage, each through a build-then-adversarial-review workflow whose
review-caught bugs are recorded below. **Cumulative poker test suite: 97 passed** at the end
of Stage 6.

| Commit | Stage | What |
| --- | --- | --- |
| `75f71a98` | design | doc/28 v2 — multiplayer design (supersedes the heads-up demo) |
| `206fff3e` | 1 (1/2) | `tests/poker_ref.py` — Python reference betting engine + side pots (the oracle) |
| `cb298ebb` | 1 (2/2) | `contracts/poker_table.py` — RV32I N-seat (2..9) betting + layered side-pots + multi-way showdown referee |
| `af7fe6a9` | 2 | `contracts/poker_deal.py` — N-player SRA/Barnett-Smart mental-poker deal; additive `FN_DECK_DIGEST` on-chain deck-digest anchoring |
| `598e0f2e` | design | doc/33 — browser wallet + injected provider design |
| `b597fc87` | 3 | Graphical SVG table SPA + non-custodial browser wallet + `window.bismuth` injected provider (`web/lib`, `web/wallet`, `web/poker/index.html`) |
| `0cfe60ae` | 4 | Append-only `TAG_RESULT` log in `_settle` + off-chain `poker_stats.py` indexer + read-only `/api/poker/*` endpoints |
| `2a750b3b` | design | doc/34 — spectator parimutuel betting design |
| `bb5c11c6` | 6 (1/2) | `tests/betting_ref.py` — parimutuel spectator side-pool reference/oracle (sequenced before the contract side) |
| `c989d1b0` | 5 | `contracts/tournament.py` — SNG + MTT (one Balancer-Vault contract, tid-namespaced copies of the §4 engine) + lobby SPA |
| `66f27bcb` | 6 (2/2) | Additive `FN_SPECTATE_BET` / `FN_SETTLE_SBETS` spectator side-pool in `poker_table.py` + spectator UI |
| `57132ae8` | wiring | `web/lib/poker-nav.js` — shared sticky cross-page nav unifying Table / Tournaments / Betting / Wallet into one app |

**Why:** prove the VM + browser-wallet stack can host a real, trust-minimized, multi-party
money game end-to-end on Bismuth.

**Verification:** each stage is differentially validated against a pure-Python oracle through
`bismuth_riscv.execute`; Stage 3's JS crypto/ABI/signer is cross-checked headless byte-for-byte
against the Python references (deal 370/370, privacy simulation 165/165, a JS-signed tx accepted
by the node's real `SignerFactory` verifier). Suite grows 39 → 68 → 86 → 97 passed across stages.

**Review bugs caught + fixed (per stage):**
- **Stage 1** — *locked funds:* an orphan side-pot layer whose contributors all fold had no
  eligible winner, so settle reverted forever and buy-ins locked; `_rebuild_pots` now cascades
  dead apex chips into the nearest lower live pot (Σ-conserving), in both oracle and contract.
  Also corrected an overstated identity claim to the engine's low-32 caller reality + added
  self-seat/collision guards (400-hand differential fuzz vs the oracle).
- **Stage 2** — caught that the heads-up `index.html` SRA modulus was a **truncated, non-prime
  224-bit literal** that breaks the roundtrip; the deal uses the true secp256k1 field prime
  (Fermat-checked at import), and the Stage-3 JS client must use the correct constant.
- **Stage 3** — *privacy break (HIGH):* the previous SPA had one coordinator compute every
  hole and broadcast cleartext cards; rebuilt as a distributed deal where each browser holds
  only its own keys (victim-key-last reveal). Plus: provider origin never `'*'`, byte-exact
  amount formatting, serialized approval modal, and sender-bound hole/board parts (stops a
  forged-strip deal wedge).
- **Stage 4** — *mis-credit (MEDIUM, hard Stage-5 prerequisite):* the result record now
  embeds the **full 28-byte address captured at settle**, so the indexer keys off the record
  (not the mutable per-seat `TAG_ADDR`) and a later seat reassignment can't mis-credit prior
  hands. Plus a copy-aliasing LOW (`account()`/`leaderboard()` deep-copy).
- **Stage 5** — *locked prize units (HIGH):* `_finalize_payout` burned the share of any
  scheduled paid place with no finisher (an MTT can legally start under-filled); fixed by
  folding every unfilled place's share into place 0 so **Σ transferred == pool**, in both
  contract and `tournament_ref.payout_split`; regression `test_mtt_underfilled_schedule_conserves`.
- **Stage 6** — *orphaned spectator funds (HIGH):* a new hand could start with the prior
  side-pool unsettled, and the lazy reset zeroed it without paying anyone; fixed via a reusable
  `_sbet_refund_all`, a refund-stale-pool lazy reset, and `FN_SETTLE_SBETS` refunding a
  past-hand pool; regression `test_unsettled_sidepool_refunded_on_next_hand`.

---

## 2. #23 — Difficulty-divergence detector + guarded self-heal (doc/35)

**What:** recurrence-prevention for the difficulty-corruption incident (the node stuck mining
at 84.48 while the network sat at ~89.8). The controller is recursive on the **stored** previous
difficulty (`difficulty.py:70/:94`), so a corrupted cached value is a self-reinforcing fixed
point that recompute-from-the-formula reproduces — only a **peer-quorum comparison** catches it,
and the only real fix is to roll back below the corruption and resync so `difficulty()` re-derives
from a clean `misc` base. The detector is an observe-only daemon (`node.py
_difficulty_divergence_loop`, core in `chain_ops.detect_difficulty_divergence`): local cached
difficulty vs a height-matched peer **median** (≥3 peers, 75% supermajority, 3-cycle debounce,
0.5-unit/2% threshold), **never scanning the ledger**. The guarded heal reuses the proven
one-shot `rollback_to` file trigger; loop-safety lives in a per-ledger `<ledger>.diffheal.json`
sidecar (cooldown, bounded depth, once-per-boot, and a **permanent lifetime cap → advisory-only
forever**).

| Commit | What |
| --- | --- |
| `e06da291` | doc/35 — design (root cause, detector, guarded heal, loop guards, test plan) |
| `8ceab9ef` | Implementation: `rest_client.get_difficulty`, `chain_ops.detect_difficulty_divergence`, the `node.py` daemon + guards — **safe by default** (detect+log only; pause-mining + autoheal opt-in) |
| `419b80a9` | Regnet 3-node soak (`tools/diff_selfheal_soak.py`) + the REST-port-resolution fix it surfaced |
| `b01ee755` | Enable `diff_divergence_autoheal` by default (operator decision, post-soak) |
| `bd65e80b` | Startup log line confirming the detector is running + its loaded flags |

**Why:** the prior incident required manual intervention; this makes accidental difficulty
self-corruption self-correcting without ever risking a restart-loop on the prod node.

**Verification:** `tests/test_difficulty_divergence.py` (23 unit tests: quorum/median/threshold,
thin-data abstain, height-mismatch, non-REST skip, every guard, plus the review regressions —
lifetime-cap-holds-past-24h, confirmed-clean-resets-budget, rest-port-failure-retries) and the
gated regnet two-node `tests/test_two_node_diff_selfheal.py`. The 3-node regnet soak verdict was
**PASS**: HEALTHY 50 cycles / 0 false positives / real ≥3 quorum, INJECT_LOCAL detected every
cycle, LYING_PEER does not flag us.

**Soak-caught bug (the soak's whole point):** the unit/two-node tests fed REST ports directly
and masked it — `peers.connection_pool` holds **socket** addresses and `/api/capabilities` isn't
served on the socket port, so `_resolve_rest_port` probing only the socket port resolved **zero**
peers and the detector would have **perpetually abstained on a real network** (inert feature).
Fixed: `_resolve_rest_port` now probes the standard `REST = socket+1` convention (mainnet
5658→5659; regnet 4060→4061) first, then the socket port, then our own REST port, trusting the
authoritative `rest_port` from the capabilities body. Regression
`test_rest_port_resolved_via_socket_plus_one_convention`.

**Review-caught guard:** a rolling-24h-window heal cap *alone* re-opens daily and would
restart-loop prod forever at 2/day; the fix is a **permanent monotonic lifetime cap (2)** that
drops the node to advisory-only forever, resetting only on a confirmed-CLEAN post-heal reading.

**Default change + prod:** `b01ee755` flips `diff_divergence_autoheal` on and `bd65e80b` adds the
startup confirmation line — both take effect on the prod node's **next restart** (the node picks
up the new defaults and the boot log line then; detect was already safe-by-default).

---

## 3. Engine-agnostic KVStore seam + all 8 LMDB store migrations (doc/26 storage stage 1)

**What:** `kvstore.py` — one small engine-agnostic KV interface + an `open_store(backend, path,
*, dbs, map_size)` factory (`backend ∈ {lmdb, mdbx, sqlite-kv}`), with a `Codec` centralizing
value (de)serialization (msgpack + JSON fallback, byte-identical to the stores' existing format).
The node's 8 LMDB stores previously each called `lmdb.open()`/`env.begin()` directly with no
shared chokepoint; switching the KV engine meant touching 8 files. All 8 now open through the
factory, so an **LMDB ↔ MDBX ↔ sqlite-kv swap is a one-arg factory change** across the whole
layer. `sqlite-kv` is always available (WAL + per-txn connections, honoring many-readers /
one-writer) and exists to *prove* swappability without an MDBX binding; `mdbx` is wired with a
lazy import.

| Commit | What |
| --- | --- |
| `d6078e58` | The `kvstore.py` seam (interface + factory + `Codec` + lmdb/sqlite-kv/mdbx backends) + migrate `reward_chain` (store 1/8, lowest-risk: non-consensus, rebuildable) |
| `40a1f646` | Migrate `txid_index` (store 2/8) + add `KVTxn.drop()` to the seam |
| `b660bc4f` | Migrate the remaining 6 — `balance_index`, `vm_state`, `token_index`, `shieldedv1`, `block_store`, `scripts/snapshot` — + add `KVTxn.count()` and `KVStore.copy_to()` |

**Why:** create a single DB-engine chokepoint so the long-tail LMDB→MDBX (or other engine)
migration becomes a configuration choice instead of an 8-file edit, without changing on-disk
formats or consensus behavior.

**Verification:** each store keeps its **public API and on-disk byte format unchanged**, so its
pre-existing parity/rebuild/rollback test passes unmodified; new `backend_swappable[lmdb|sqlite-kv]`
tests run the *same* store on both engines and assert identical results; on-disk bytes pinned
byte-identical against a raw-lmdb read; a best-of-N perf gate confirms the LMDB wrapper is a thin
passthrough (range ~+7%, get within noise). `tests/test_kvstore.py` plus the per-store tests:
**full storage suite 62 passed**; no `import lmdb` / `lmdb.open()` remains in any store file
(only `kvstore.py`'s `LmdbKVStore` adapter).

**Honest scope (deferred):** the consensus SQL paths (`ledger_balance3`,
`_signature_exists_in_ledger`) and the SQLite `commit_marker`/ATTACH/WAL lockstep write path are
**byte-untouched** and stay deliberately on SQLite (doc/26 defers them; inflation risk). This is
the engine-seam stage, not the SQLite retirement.

---

## 4. Multi-node verification of the migrated stores + shield/token placement (doc/12, 22)

After the engine-seam migration (§3), the migrated stores were validated **across independent nodes**,
not only in single-process unit tests — "do two nodes that built their stores independently agree?" is
the question that actually matters for consensus.

| Commit | What |
| --- | --- |
| `49ea7e03` | `tests/test_multinode_integration.py` — a **3-node** regnet cluster: A mines a fork-crossing chain; B and C start with empty ledgers and reconstruct it over REST `api_sync` (regnet refuses socket peering by design). Asserts blocks `1..tip` are **byte-identical** across A/B/C, and that each migrated KVStore store agrees cross-node — `block_store` bodies, `balance_index`, `txid_index` heights, `vm_state` root + a deployed RISC-V contract's storage — plus the #23 detector reads CLEAN. Gated behind `BISMUTH_RUN_MULTINODE=1`. |
| `afe4cd39` | Docstring fix: the header had lumped `shieldedv1` + `token_index` in with the four core KVStore side-indexes. Corrected — `shieldedv1` is **core** (opened in `node.py`, consensus-wired); `token_index` is owned by the **`tokens_aliases` plugin** (doc/27), never constructed by node core. |
| `7c7c5888` | **Populated-state** coverage for the two flag-gated stores: node A (post-fork) issues + transfers a token and `shield:mint`s a note, then the test asserts `/api/token/<name>` (supply + holder set) and `/api/shield/stats` (notes/key_images/pool_units + the doc/22 `pool == sink` supply-safety invariant + the specific note record) are **byte-identical across A/B/C**. A coverage guard *requires* the population to have landed when the fork is active, so the check can't silently degrade to a vacuous empty-state pass. |

**Shield-vs-token placement (clarified, not changed).** A code-and-docs investigation this session
confirmed the current architecture is correct and matches intent: **shielded value is core** and
consensus-wired (`shieldedv1.py`, imported by `digest.validate_block`, doc/22); **tokens/aliases are a
plugin** (`plugins/tokens_aliases`, doc/27 — a consensus-inert projection node core only defers to);
**both default off** (`shield=False`, `token_index=False` in `options.py`) and are hf2-height-gated
inert pre-fork. The `shieldedv1` name is a consensus-protocol **generation** marker, not a
prototype/maturity flag (see doc/22 "Naming & placement"). **No code change was warranted.**

**Verification:** the gated 3-node test passes (`BISMUTH_RUN_MULTINODE=1 python3 -m pytest
tests/test_multinode_integration.py`) — re-run several times (~10–22 s), with the populated token/shield
checks firing on real values (token supply 1,000,000 + per-holder balances; shield pool = 20 BIS, the
`pool == sink` invariant). The **full suite is green: 790 passed, 6 skipped, 0 failed** (8.5 min, run
**serially** so a single regnet test node never I/O-starves the live prod mainnet node). The 6 skips are
exactly the env-gated multi-process node tests (`BISMUTH_RUN_MULTINODE` ×1, `BISMUTH_RUN_TWONODE` ×5),
deliberately off in the normal suite; the prod node stayed synced throughout.

---

## 5. Prod slow-resume fix + heavy-query audit (doc/37)

A mainnet restart took ~13 min to resume syncing. Root-caused from the boot log (the recompression guess
was **refuted** — skipped in ~2 ms) to two unrelated causes, both fixed, then a codebase-wide audit of
similar heavy queries (worse as the chain grows post-fork).

| Commit | What |
| --- | --- |
| `196f8998` | worker.py: cap the outbound dial at `DIAL_TIMEOUT=5s` (clearnet) — a dead peer no longer hangs the worker in `connect()` for the OS SYN-retry window (~127s), the ~9.6 min "stuck 1 block behind" phase. chain_ops.sequencing_check: bound the `misc` difficulty scan to `max(300000, sequencing_last)` instead of re-reading 300000→tip every boot (~3.5 min). |
| `9324ffaa` | Behaviour-preserving scan-narrowing: `balance_index`/`txid_index` rebuilds `SELECT *`→only the needed columns (the dominant boot cost; no longer drags every row's pubkey/sig blobs through Python); REST `/api/transaction` signature fallback → `TXID4_Index` seek instead of a full-table `LIKE`; `balanceget` 3→2 full-history scans. |
| `cc559849` | **doc/37** — the standing heavy-query reference: index baseline, fixes, the `ledger_balance3` consensus guardrail, deferred items with rationale, the post-fork degradation watchlist, and 2 correctness blockers (recompress not integer-safe; balances blanket-except). |

**Verification:** storage parity 18/18 + balanceget/tx-lookup/REST 40/40; the full suite stayed green; prod synced throughout (audit was source-only — no prod-ledger scan).

## 6. Config modernized to TOML (doc/11)

| Commit | What |
| --- | --- |
| `96fe943f` | `options.py` reads **`config.toml`** via stdlib `tomllib` (zero new runtime dep), preferring `config.toml`/`config_custom.toml` with a **byte-identical `config.txt` fallback** (same precedence, same `BISMUTH_IGNORE_CONFIG_CUSTOM` gate, same `Config` surface → no consumer changes). `scripts/migrate_config.py` is a non-destructive, self-verifying migrator; `config.toml.example` generated from it; 6/6 hermetic tests; doc/11. |

**Why TOML, not YAML:** `tomllib` is stdlib (3.11+), so reading adds no dependency; YAML would add PyYAML and its coercion footguns (Norway `no`→False) on consensus-affecting flags.

## 7. Tor/onion modernized + bundled via the installer (doc/38)

| Commit | What |
| --- | --- |
| `706ea79d` | The outbound-only `setproxy(9050)` bolt-on → a tri-state `tor` (off/external/**managed**) in a new `tor_manager.py`: stem-launched tor + ephemeral v3 onion + auto-SOCKS + bootstrap gating + graceful clearnet fallback (or `tor_required` fail-fast). Single proxy source-of-truth (off → byte-identical clearnet). 20/20 tests (managed via a mocked controller — no real tor in CI). |
| _(this batch)_ | `install_node.sh --tor` installs the distro `tor` package + `stem` (we do NOT vendor a binary — the distro package gets security updates). **doc/38** + `web/site/index.html` "Run a node" rewritten to the one-command installer with auto-bootstrap (the obsolete manual ledger-download / pip steps removed). |

**Honest scope:** real onion routing still needs the tor C binary + the live Tor network; "managed" only removes the manual torrc/daemon burden. Validate managed mode against a real tor before production; `external` mode preserves the field-proven behaviour.

---

### Notes
- The KVStore seam now has its own page: **[doc/36](36-kvstore-engine-seam.md)** (the definitive
  reference for `kvstore.py` — interface, factory, backends, the migration table, and what is *not*
  abstracted). Its design is also captured in `kvstore.py`'s module docstring and framed as storage
  stage 1 of doc/26.
- Cross-references: poker → doc/28, 32, 33, 34; difficulty self-heal → doc/35; KVStore seam → doc/36;
  storage → doc/26 (and the SQLite→LMDB canonical migration / storage stages tracked in doc/17, 26).
- The full session map lives in **[doc/00-architecture-overview.md](00-architecture-overview.md)**.
