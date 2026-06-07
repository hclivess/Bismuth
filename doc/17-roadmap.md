# 17 — Roadmap

> The plan for modernizing the Bismuth node. This is the high-level companion to the database deep-dive
> in [`16-database-rework-plan.md`](16-database-rework-plan.md); read that for the storage details.

## Guiding principles

1. **Consensus does not change.** The same blocks must produce the same hashes and validate
   identically. The exact signing-buffer and block-hash byte forms are frozen in `bismuth_serialize.py`
   and characterization-locked; everything else (storage, transport, APIs) is free to change behind
   that boundary.
2. **Replay-verified.** Every storage/representation change is validated by re-hashing the chain
   end-to-end through the frozen boundary (`replay_verify.py`, `tests/test_replay.py`) and must produce
   **byte-identical** block hashes. No silent drift.
3. **Incremental and reversible.** Small, test-gated changes — not a big abstract rewrite. New
   behaviour is opt-in (config-flagged, default off) until validated on regnet and by independent
   replay. Old peers keep working.
4. **Modernize at the edges, freeze the core.** New, clean modules at the seams; surgical edits to the
   legacy monoliths; the consensus core is touched only through the frozen layer.

## ✅ Done

- **Consensus-serialization freeze.** `bismuth_serialize.py` holds the frozen signing/block-hash byte
  forms; `digest`, `mempool` and signing all route through it. Locked by `tests/test_characterization.py`.
- **Schema versioning & migrations.** `db_migrations.py` applies ordered, idempotent migrations tracked
  by `PRAGMA user_version`.
- **Integer atomic-unit storage (phase 2).** `amounts.py` provides the exact decimal↔integer-units
  converter; `replay_verify.py` proves integer round-tripping changes no block hash; `migrate_amounts.py`
  is the offline column migration. The live cutover is **enabled on regnet** behind the
  `ledger_integer_amounts` flag (default **off**, so mainnet is untouched). Display/consensus edges all
  reconstruct the legacy decimal strings at the boundary.
- **Balance cache (phase 4, safe slice).** `balance_cache.py` memoizes the authoritative balance per
  `(address, height)`; auto-invalidates on height change.
- **Modern HTTP layer.**
  - Read-only **REST API** (`rest_api.py`): status, blocks (single / since / range), balance,
    transaction, address history, mempool, peers; a self-describing welcome index.
  - **Capability discovery** (`GET /api/capabilities`): reachability *is* the test — if a peer's API
    answers, it is REST-capable, and advertises its rest port + negotiable codecs.
  - **Parallel block fetch** (`rest_client.py`): concurrent, compressed `/api/blocks/range` chunks —
    the performant alternative to the serial socket sync. Fails soft → falls back to sockets.
  - **HTTP transport compression**: `gzip`/`br` via `Accept-Encoding` (codecs from `transport.py`,
    zero hard native deps); `?compress=none|gzip|br` override for explicit plaintext/codec.
  - **Version bump** `mainnet0023` as the modern-capabilities signal (not a consensus change —
    nothing in `digest`/`bismuth_serialize` gates on the version string).
- **Bootstrap resilience (operational hardening).** `chain_ops.bootstrap` no longer depends on a single
  hardcoded download host (which can and did vanish). It prefers a locally-provided ledger archive
  (`bootstrap_file` config, or an archive dropped at `<ledger_path>.tar.gz`), downloads from a
  configurable `bootstrap_url` only as a fallback, extracts into the ledger's own directory, and
  surfaces failures loudly instead of swallowing them in a bare `except`. Covered by
  `tests/test_bootstrap_local.py` (no network, no node subprocess).
- **Legacy wire-compatibility verified live.** `legacy_sync_probe.py` (read-only) confirms this codebase
  still speaks the legacy socket protocol to the current mainnet: version handshake → `ok`, compatible
  peer versions (`mainnet0021/0022`), peer-list exchange (≈500 peers) and block-height negotiation all
  succeed against live peers (tip **4,845,284**). Note: legacy peers reject ancient checkpoints
  (`blocknf`/rollback), so a from-genesis forward sync is not served — a bootstrap snapshot is required,
  which is exactly what the resilience work above makes dependable.
- **Non-RSA signer deps are mandatory for a node (the real sync-blocker).** mainnet carries ECDSA and
  ED25519 transactions, so a node missing `coincurve`/`ed25519` rejects **every** such block
  (`ModuleNotFoundError` in `polysign/signer_*`) and silently stalls at the first one. This — not "slow
  sync" — is what stranded a freshly bootstrapped node (it had connected to real peers and they were
  delivering blocks; the node rejected them all). The improved digest logging above is what revealed it.
  `requirements*.txt` now mark these **required** (were wrongly "optional"), with the Python-3.12
  `ed25519` build caveat documented (its bundled versioneer uses configparser APIs removed in 3.12).
  With the dependency present the node syncs the legacy socket path at **~16 blocks/s**.

## ◑ In progress / next

- **API-based sync.** Wire `rest_client.parallel_fetch` into the actual catch-up path so a node syncs
  over the HTTP API when a peer is REST-capable, instead of the serial, blocking, no-asyncio socket
  loop (`connections.py`) that stalls. The socket protocol stays for old peers; it is no longer where
  new capability is added.
  - ✅ **Headers-first + consensus-faithful body mapping done.** `GET /api/headers/range` serves the
    cheap header chain (height/hash/timestamp/txs) for a Bitcoin-style first pass; `GET
    /api/blocks/range?format=sync` serves digester-ready tuples that keep the public key **base64 as
    stored** (the display API decodes it, which would corrupt the signed bytes — the mapping hazard
    that blocked this). `rest_client` gains `fetch_headers` / `parallel_fetch_sync` /
    `blocks_to_digester` / `headers_are_contiguous`. `tests/test_headers_sync.py` proves every signed
    tx still verifies through the sync serialization and each body re-hashes to its header — so the
    blocks can be fed straight to the digester. This removes the "careful REST-block → digester
    mapping" risk.
  - ◻ **Remaining: the live wiring** — call this path in the catch-up loop behind a config flag
    (default off) and validate node-to-node. Needs a two-node harness; the single-node regnet suite
    can't exercise one node ingesting another's blocks. **Reality check (measured on mainnet 2026-06):**
    no live peer is REST-capable yet (legacy peers expose only the socket port), so this path helps
    only once peers upgrade and lets THIS node *serve* fast sync. Live catch-up against the current
    network is stuck on the legacy socket protocol (measured ≈0.8 blocks/s, bursty, ~4 min to ramp
    peer connections) — improvable only by tuning that protocol (see the legacy-stack item below).
- **Incremental balance index (phase 4 deep).** A maintained O(1) credit/debit index, updated on
  apply/rollback, that bit-matches the authoritative computation. Depends on integer storage.
- **Explicit reward & pruning model (phase 5).** Replace negative-height "mirror" reward rows and
  synthetic `address='Hyperblock'` rows with real columns/tables; convert `recompress_ledger` and the
  hypernode/`ledger_queries` paths to integer units (currently left legacy, tagged `# HARDFORK`).

## 🗺️ Planned

- **Consensus hard fork.** Change the consensus serialization itself to sign/hash **native integer
  units + a binary/struct tx encoding**, deleting the `'%.8f'`/`'%.2f'` string reconstructions (every
  site is tagged `# HARDFORK (doc/16)` — `grep -rn "HARDFORK (doc/16)"`). Adopt a bounded,
  content-derived **txid** (nado-style: `blake2b(tx_content)`, the signature signs the txid) to replace
  the ad-hoc `signature[:56]` slice. After the fork, storage/boundary/APIs are integer end-to-end.
- **Difficulty-retarget rework.** The current retarget (`difficulty.py:difficulty()`) needs replacing:
  the **per-block jumps are too steep** and the **approach is convoluted**. Concretely — the steep
  jumps: a single block can move difficulty by up to `MAX_DIFF_ADJUST = 1.0` in the log2-style
  difficulty domain, i.e. a **full doubling of work in one block**; the upward step is capped but the
  downward path is not (uncapped `diff_adjustment` plus a separate wall-clock "emergency diff drop"
  ramp), so it is asymmetric; and the derivative term `Kd·(block_time − block_time_prev)` with
  `KD_GAIN = 10` amplifies noisy block-time samples into large swings. The weird approach: instead of
  the standard "actual vs. target timespan ratio" retarget, it **estimates hashrate from the previous
  difficulty and then inverts that estimate** to back out a new difficulty, through opaque magic
  constants (`28`, `/16`, `/720`); it layers a second control path (`diff_dropped`, the broadcast
  difficulty that decays with wall-clock time since the last block) on top; and it wraps the whole
  thing in a bare `except:` that silently resets difficulty to a hardcoded `[24,…]` on **any** error.
  Target a single, well-understood, bounded controller (smooth, **symmetric** per-block clamping; one
  difficulty value, not a retarget + a separate drop ramp; explicit named constants; no error-swallow).
  This is **consensus** — `mining_heavy3.check_block` validates blocks against the retarget — so it is a
  **hard fork**, gated and replay-validated like the items above, not a quiet swap.
- **Supersede the legacy socket / peer / block-processing stack with the API system.** This is the
  project's worst code — blocking, no asyncio, stall-prone — and the long-term plan moves its capability
  onto the HTTP/REST API (parallel, compressed, non-stalling). That replacement is the *destination*, but
  it is **not** a reason to leave the code frozen in the meantime: these modules are **legitimate targets
  for behavior-preserving modularization and cleanup now — they are not off-limits.**
  - **Connectivity & peers** (`connections.py`, `connectionmanager.py`, `worker.py`, `peershandler.py`) —
    to be superseded by the REST API, but fair game for modularization/cleanup until then.
  - **Block processing** (`digest.py`) — needs a major rework and will not be carried into the API system
    (the API sync path does its own block ingestion), but it can and should be modularized/cleaned behind
    the frozen consensus boundary in the meantime.
  - **Command dispatch** (`node.py` `handle()` and the `commands.py` CLI wrapper) — the legacy socket
    command path; capability moves to REST over time. `handle()` still needs a two-node harness before
    its `if/elif` chain becomes a dispatch table (see the modularization note below); `commands.py` can be
    modularized independently right now.
  Keep a compatibility bridge while the network upgrades; build new capability only on the API path.
- **Mempool anti-spam — economic/resource-based, never identity-based.** Anyone can mint unlimited
  addresses, so **per-address caps are Sybil-trivial** and a false comfort — do not add them. What
  already works and must be kept: every tx pays a fee out of the sender's *funded* balance (`merge`'s
  balance check), so flooding from N addresses costs N×fee in real BIS spread across funded addresses;
  and total mempool size is bounded (`space_left_for_tx`, ~0.6 MB). The gaps to close:
  1. ✅ **Done.** The congestion-prioritisation tiers in `space_left_for_tx` admitted by nominal
     `amount` (a spammer self-sends a large amount for the price of one base fee) and by a config
     address allow-list (Sybil-trivial). Both are gone: admission is now gated by the tx's actual
     **deterministic fee** (`fee_calculate` — base + openfield length + token/alias surcharge, the one
     thing a spammer cannot inflate without paying it), in successive bands (`> base`, `>= 1`, `>= 10`).
     The hard protections (every tx pays a fee from a *funded* balance; total pool bounded) are
     untouched in `merge`. Covered by `tests/test_mempool_antispam.py`. The `mempool_allowed` config
     option is left defined but unused (back-compat).
  2. Put **rate limiting on the HTTP ingestion layer** when tx submission moves to the REST API (the
     survivor path) — per-connection/token throttling + HTTP 429 — rather than bolting policy onto the
     doomed socket `merge`.
  3. If economic + rate limits prove insufficient, a structural fix (small **PoW-per-tx**, or a real
     fee market) is a hard-fork consideration. Consensus tx-validity stays unchanged; only *local
     admission* tightens.
- **Storage-engine evaluation (phase 7).** Modernize SQLite usage (WAL, integer keys, covering
  indexes), benchmark, then consider a KV store (LMDB/RocksDB) for block bodies while keeping SQLite
  for queryable indexes. Decide on data, not taste.
- **Repository reorganization & modularization.** Retire dead files (✅ done — moved to `attic/` by
  import-graph analysis) and break up the over-long modules behind **behavior-preserving, test-green**
  extractions. `node.py` (~2.2k lines, with a ~1080-line socket-command `handle()`) is the prime
  target: turn the `if/elif` command chain into a dispatch table of small handlers, and lift the
  bootup/init and ledger-maintenance functions into focused modules (most already take `node`
  explicitly, so they extract by dependency injection). Cuts done so far: the 837-line `apihandler.py`
  god-class split into domain mixins (`apihandler_blocks`/`_address`/`_tx`) recombined via
  `class ApiHandler(BlockApiMixin, …)`, leaving a 115-line dispatcher core (its pure block→JSON
  formatters had already moved to `block_format.py`); the 498-line `dbhandler.py` (a survivor — storage)
  likewise split into `DbQueriesMixin` (`dbhandler_queries`) + `DbWriteMixin` (`dbhandler_write`),
  leaving a 166-line connection/plumbing core that keeps the canonical `sql_trace_callback`; the 714-line
  `mempool.py` (also a survivor) split into `mempool_sql.py` (SQL + tuning constants), a
  `MempoolQueriesMixin` (`mempool_queries`) for read/reporting/maintenance, and a 499-line core that
  keeps DB plumbing + the consensus `merge`; and the chain-maintenance cluster
  (`rollback`, `recompress_ledger`, `ledger_check_heights`, `blocknf`, plus the boot/validation
  `bootstrap`, `check_integrity`, `sequencing_check`) lifted out of `node.py` into `chain_ops.py` by
  DI, with `blocknf` re-exported so `worker.py` is unaffected; the mempool-aware `balanceget` moved to
  `balances.py`; and the bootstrap/init helpers (`setup_net_type`, `node_block_init`, `ram_init`,
  `initial_db_check`, `load_keys`, `add_indices`) lifted into `node_init.py` by DI (the consensus chain
  `verify` deliberately stays); and the wallet/key-management cluster (`sign_rsa` + the `keys_*`
  load/save/unlock functions) lifted out of the 415-line `essentials.py` into `wallet_helpers.py`
  (re-bound on `essentials` for back-compat, so every `essentials.keys_load` / `from essentials import …`
  call site is unchanged), leaving `essentials.py` at ~280 lines of pure helpers; and — now that the
  legacy peer stack is no longer off-limits — the 550-line `peershandler.py` `Peers` god-class split into
  four domain mixins (`peers_storage` / `peers_pool` / `peers_consensus` / `peers_access`) recombined via
  `class Peers(PeersStorageMixin, …)`, leaving a ~190-line core (`__slots__` + `__init__` + net-type
  helpers + the `client_loop` maintenance orchestrator); all 31 method bodies stayed byte-identical and
  the mixins carry `__slots__ = ()` so the slotted layout is preserved; and the 660-line `digest.py`
  consensus pipeline had its block/tx **data model** (`Transaction` / `MinerTransaction` / `Block` value
  objects + the local consensus quantizers, which intentionally differ from `quantizer.py`) lifted into
  `digest_tx.py`, leaving the `BlockProcessor` engine + `digest_block` orchestration in a ~527-line core
  — all five moved nodes byte-identical and `tests/test_replay.py` confirms the chain re-hashes the same.
  **`node.py` is down from 2200 to ~1420 lines (−36%).** The remaining
  bulk is the ~1080-line socket-command `handle()`: its branches use `break`/`continue` against the
  connection loop and interleave consensus-critical sync, so turning it into a dispatch table needs a
  two-node test harness first (tracked above under "API-based sync"), not a blind rewrite. Each step
  keeps the flat-import layout working and the suite green.

## How we work

- Branch behaviour behind a config flag; keep it default-off until regnet + replay validate it.
- Never merge a change whose replayed block hashes differ.
- Test every change end to end — the suite launches a real regnet node; new behaviour ships with tests.
- Prefer surgical edits and small clean modules over sweeping abstract rewrites.
