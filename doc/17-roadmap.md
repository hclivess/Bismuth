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
- **Connectivity reliability fixes (operational, unit-tested).** Live bring-up surfaced and fixed:
  a node **dialling itself** (`127.0.0.1` ships in default `peers.txt` *and* is whitelisted) and then
  reporting a false "100 % consensus" off its own height — `can_connect_to` now refuses our own
  listening address; an over-aggressive connection **back-off** (5–30 min cooldowns that `reset_tried`
  kept instead of clearing) that stalled catch-up — now gentle/capped (30 s/1 m/2 m/5 m) with a 60 s
  starved-reset; and the **bare-`print` digest error handler** (`<class 'ValueError'> digest.py 327`)
  now logs the real failure site + cause (this is what exposed the ed25519 sync-blocker). With these +
  the dependency fix, **a fresh node bootstraps and syncs to the mainnet tip end-to-end** (verified live:
  4.72M snapshot → **4,845,489 tip**, clean, no rejections).
- **Bootstrap is hosted again.** `https://bismuth.cz/ledger.tar.gz` — the node's historical default
  `bootstrap_url`, on a rebuilt host (nginx + Let's Encrypt) — now serves a fresh **at-tip** snapshot
  (ledger + hyper + index, ~5.9 GB, resumable byte-range), produced from the live node with zero downtime
  via SQLite online backup (`_mkbootstrap.sh`). So the dead-host failure is resolved in practice, not
  only in code.

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
  synthetic `address='Hyperblock'` rows with real, queryable structures; convert `recompress_ledger`
  and the hypernode/`ledger_queries` paths to integer units (currently left legacy, tagged `# HARDFORK`).
  - ✅ **Reward sidechain foundation** (`reward_chain.py`): the dev-fund + hypernode-payout rewards are
    minted LOCALLY at commit (`dbhandler_write.dev_reward`/`hn_reward`) as negative-height rows in the
    main ledger — they are **not synced**. `RewardChain` lifts them into a separate store keyed by the
    POSITIVE block height, preserving each row's exact balance effect (minting source debited, recipient
    credited). `tests/test_reward_chain.py` proves `(main positive ledger) + (reward sidechain)` equals
    today's full-ledger balances for **every** address, on synthetic and real regnet data — so the move
    is **balance-preserving and replay-identical** (synced bodies/hashes unchanged → no consensus
    change). ◻ Remaining: wire the digester to write rewards here instead of negative rows, and the
    balance path (incl. the balance index) to read them, behind a config flag, replay-validated.

## 🗺️ Planned

- **Consensus hard fork.** Change the consensus serialization itself to sign/hash **native integer
  units + a binary/struct tx encoding**, deleting the `'%.8f'`/`'%.2f'` string reconstructions (every
  site is tagged `# HARDFORK (doc/16)` — `grep -rn "HARDFORK (doc/16)"`). Adopt a bounded,
  content-derived **txid** (nado-style: `blake2b(tx_content)`, the signature signs the txid) to replace
  the ad-hoc `signature[:56]` slice. After the fork, storage/boundary/APIs are integer end-to-end.
  - ✅ **Content-hash txid + single-sig recoverable signature — IMPLEMENTED + tested.** Post-fork the
    canonical id is the content-hash txid — `blake2b-256` of the same frozen pre-image consensus signs
    (timestamp/address/recipient/amount/operation/openfield). It is computed **ON READ**
    (`essentials.format_raw_tx`, amount via `amounts.ledger_value` so it is **storage-mode agnostic** —
    integer-units rows and legacy decimal rows both normalise back to the canonical `'%.8f'` signed
    string). There is **no new `txid` DB column and no migration** — lookup is **shape-dispatched**: a
    64-char lowercase-hex query resolves the content txid by scanning post-fork rows, anything else
    falls through to the legacy signature-prefix `LIKE` match (`rest_api._transaction`). **Scope: only
    ordinary single-sig secp256k1 uses the post-fork recoverable-signature path** — it signs the 32-byte
    content txid, carries a 65-byte recoverable hex signature, **drops the `public_key` field**, recovers
    the signer via `ecrecover`, and enforces **low-s** (`signer_ecdsa.sign_buffer_for_bis_recoverable` /
    `verify_recoverable`). **RSA, ED25519, native MULTISIG, and shielded/RingCT keep their existing
    legacy signing** post-fork (multisig: explicit pubkeys + N-of-M over the frozen buffer — it does
    **not** sign the txid). **ALL post-fork txs still receive the content-hash txid as their canonical
    id.** Pre-fork is byte-identical; historical txs keep their `signature[:56]` ids. Covered by
    `tests/test_hf2_recoverable.py` and `tests/test_hf2_fork_transition.py`.
  - **Signature & public-key storage optimization (a stated goal for the fork).** The 1068-byte base64
    RSA public key is carried and stored on **every** transaction, as is the 684-byte base64 signature —
    together the dominant part of a block body. The fork should: (a) carry the public key by
    **reference** — a sender's address is the hash of its key, so after an address's first appearance the
    full key need never be repeated (a per-address key table); (b) store keys/signatures as **raw bytes**
    rather than base64 (~25 % smaller for RSA, and far smaller for the 32–33-byte ECDSA/ED25519 keys and
    64-byte sigs); (c) keep only the content-hash txid + signature on-chain. The same dedup can be done
    **pre-fork, locally** in the LMDB block store (transparent, lossless — the measured 4.3 GB→ below
    SQLite); the fork makes it canonical and network-wide.
  - **Coinbase compaction.** The coinbase (miner reward) is ingested as a *full* signed transaction — a
    1068-byte RSA public key + 684-byte signature like any tx, plus (a Bismuth quirk) the PoW **nonce in
    its openfield** (which the hf2 readiness signal currently rides in). Wasteful: the reward is
    consensus-derived (needs no repeated key) and the nonce deserves its own field. The fork should give
    the coinbase a compact, purpose-built encoding (Bitcoin-style special coinbase).

  The current retarget (`difficulty.py:difficulty()`) needs replacing:
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
  **hard fork**, gated and replay-validated like the items above, not a quiet swap. ✅ The replacement
  is **implemented + unit-tested** (`difficulty_lwma.py`, `tests/test_difficulty_lwma.py`): LWMA —
  symmetric (slow blocks lower difficulty by the same law fast blocks raise it), **delicate** (~1 %
  block-time nudge per step under normal variance, never a cliff), and **deterministically calculable**
  by every miner in advance from public chain data (one knowable challenge for all). Inert until the
  fork gate (see [`18-hardfork-hf2.md`](18-hardfork-hf2.md)).
- **External-ecosystem RPC compatibility (edge adapters, non-consensus).** Plug Bismuth into standard
  tooling without bespoke integrations, beside `rest_api.py`, behind flags:
  - **Bitcoin-compatible JSON-RPC** — translate the core `bitcoind` methods (`getblockcount`,
    `getblockhash`/`getblock`, `getbalance`, `getnewaddress`, `sendtoaddress`, `sendrawtransaction`,
    `getrawtransaction`, `gettransaction`) onto the ledger + wallet; enough for most exchange/explorer
    integrations.
  - **Ethereum/ERC-compatible interface** — the `eth_*` JSON-RPC subset (`eth_blockNumber`,
    `eth_getBalance`, `eth_getTransactionByHash`, `eth_sendRawTransaction`) + ERC-20-style views of the
    token layer, for web3/wallet tooling. Honestly bounded: Ethereum's account/gas/EVM model differs
    fundamentally, so this is a **compatibility shim** (balances, tx submit, blocks, token views),
    **not** an EVM — full smart-contract semantics are out of scope.
- **Decentralized-apps v2 protocol — on-chain VM execution (the real smart-contract layer).** What the
  shim above deliberately *isn't*: transactions carry program/call data, a **deterministic virtual
  machine** executes it on every node, and the resulting **state + output are written back to the chain
  and agreed by consensus** — the Ethereum model. A major, consensus-level addition layered on the hf2
  binary tx encoding. Building blocks:
  - a **sandboxed deterministic VM** — SHIPPED as a single **RISC-V (RV32I)** interpreter
    (`bismuth_riscv.py`; Vitalik's "RISC-V over the EVM" direction, a frozen ISA with real C/Rust
    toolchains) rather than a bespoke bytecode or WASM; *every node must execute byte-identically*, so
    determinism is the whole game (integer-only, no wall-clock, fixed iteration order, bounded memory);
  - **gas / metering** to bound execution and halt runaway or adversarial programs;
  - a **contract state trie committed into the block hash**, so state is verifiable and replay-checkable
    like everything else behind the frozen boundary;
  - a deploy/call **tx format** + a contract-storage model, generalising today's openfield + token layer.
  - **Security is the hard part and designed-against from day one** (per the standing attack-vector
    rule): reentrancy, integer overflow/underflow, **gas-exhaustion DoS**, and — uniquely fatal for a
    chain — any **non-determinism** (floats, time, map ordering, uninitialised memory) that makes two
    honest nodes disagree and forks the network. Also state-bloat / unbounded growth. This is its own
    multi-stage effort and its own hard fork, well beyond the storage/serialization work.

  **Status (built + regnet-tested):** a deterministic **RISC-V** engine (`bismuth_riscv.py`, RV32I), a
  contract-state store (`vm_state.py`: code + storage + custody balances), `vm:deploy`/`vm:call` parsing +
  execution (`vm_engine.py`) GATED behind hf2 in the digest, and `/api/vm/*` for the explorer — all behind
  the `vm` flag, inert until the fork. Execution is main-layer (every node runs it).
  - **State root — DONE + ENFORCED:** a deterministic hash of all contract state (code + storage +
    balances) is committed per post-fork block; the miner embeds it in the coinbase and the digester
    REJECTS a block whose committed root disagrees — a non-determinism bug is a *caught* block-rejection,
    not a silent divergence. (doc/19)
  - **HTLC / atomic swaps — the flagship app; building blocks complete, contract still to write.**
    Bitcoin-style hash-time-locked contracts (BIP-199) are now *buildable*: `SYS_SHA256` preimage claim +
    block-height `SYS_NUMBER` timelock refund, and **value custody is DONE** — contracts hold and release
    real BIS rollback-deterministically (balance in `vm_state`, settled via a custody sink) — so trustless
    BIS↔BTC/LTC/DOGE swaps need no bridge or sequencer. The standalone HTLC contract itself is not yet in
    `contracts/` ([24](24-defi-dex.md) tracks it as planned).
  - **Engine:** a SINGLE deterministic **RISC-V** (RV32I) interpreter — Vitalik's direction, real C/Rust
    toolchains, a frozen ISA. The state/gate/rollback/root framework is engine-agnostic, but there is one
    engine now, no dispatch.
- **Rust client (second, independent implementation).** A Bismuth node/client in Rust, built to the
  same frozen consensus boundary (`bismuth_serialize.py`'s signing/block-hash byte forms) and
  **replay-validated to produce byte-identical block hashes** against the Python node — a second
  implementation is the strongest test that the consensus rules are actually *specified*, not merely
  "whatever the Python does." Motivations: throughput (the blocking, no-asyncio socket/digest stack is
  the measured bottleneck), memory safety, and a real toolchain for the RV32I contract VM (this doc's
  "real C/Rust toolchains" direction — the same RISC-V ISA the demo contracts target). Staged so it
  never forks the network:
  1. **Read / validating client** — sync (REST first, socket fallback), verify PoW + the Heavy3 hash +
     signatures (RSA/ECDSA/ED25519, polysign-equivalent) + the difficulty retarget, and re-hash every
     block through the frozen serialization, asserting parity with the Python node on the live chain.
  2. **Wallet + tx submission** — key load/sign for all signer types, mempool submit over REST.
  3. **Full validating node** — mempool, digest, peer serving; able to mine and be mined against,
     cross-validated node-to-node with the Python node before it carries any real peering.
  Consensus-critical paths (Heavy3 PoW, LWMA/legacy retarget, serialization, signature schemes, and —
  post-fork — the RV32I VM and shielded ring-sig verification) are **ported with parity tests, never
  reinterpreted**. Default-off and non-authoritative until it matches Python block-for-block.
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
- **Scalable storage architecture (phase 7) — the path to a *fully scalable* DB.** Benchmarked on the
  live mainnet ledger (**22 GB, block 4.85M, 7.13M txs**): indexed point lookups (block by height / by
  hash) are sub-second, but a **per-address balance aggregate is a full-table scan that did not finish
  within 200 s** — that is the scalability wall, and `ledger.db` also grows without bound. The target,
  built **behind the frozen serialization boundary so block hashes stay byte-identical — replay-validated,
  no hard fork**:
  1. **Immutable block bodies → an append-only KV store (LMDB).** ✅ **Foundation implemented**
     (`block_store.py`): an LMDB `BlockStore` with two sub-dbs — `blocks` (big-endian uint64 height →
     msgpack `{hash, txs}`) and `hashes` (block_hash → height) — giving O(1) tip, numeric-ordered range
     scans, `height_by_hash`, and height-based `rollback` (with hash-index cleanup). Each stored tx
     drops the redundant block_height (it is the key); `get_block` re-prepends it, so callers get the
     exact 12-field ledger rows back — a **lossless mirror**, proven by `build_from_sqlite` +
     `verify_against_sqlite` and `tests/test_block_store.py`. Storage only, behind the frozen boundary.
     **Validated at scale** (`_lmdb_demo.py`): 995,531 real mainnet blocks (1.15M txs) built in 89 s
     (~11k blk/s), byte-identical to the ledger, and every signed tx re-verified through the store (0
     failures). With **public-key dedup** (each distinct key stored once, keyed by a blake2b hash since
     a 1068-byte key exceeds LMDB's 511-byte key limit, referenced by a small id), the footprint dropped
     from 4.3 GB to **1.73 GB** for those 1M blocks — **~2x smaller than SQLite** (~3.5 GB for the same
     txs) and still byte-for-byte lossless. So the store now wins on size *and* architecturally — O(1)
     keyed access, append-only (no VACUUM/fragmentation), and (step 2) maintained balances instead of
     the >200 s full scan. (Signature raw-bytes packing is deferred — some sigs aren't valid base64; the
     canonical sig/pubkey encoding belongs to the hard fork.)
     Chosen **LMDB over RocksDB:** a single mmap'd file, no background compaction, copy-on-write MVCC
     reads alongside a single writer — a near-perfect fit for an append-heavy, read-heavy, immutable
     workload, and a trivial embedded dependency (`pip install lmdb msgpack`). ✅ **Shadow-write
     integration done** (behind the `block_store` flag, default off): the digester mirrors each
     committed block into the store (additive, best-effort, AFTER the normal commit) with a matching
     rollback hook. The whole regnet suite runs with it ON — including `test_replay` (byte-identical
     chain), `test_characterization`, and the consensus invariants — proving it is additive and
     **mining-invariant** (the block hash / validation path is untouched). ◻ Remaining: switch READS
     to the store (sync serving + queries) behind the flag, then make it primary.
  2. **Maintained incremental indexes for queryable state.** ✅ **Balance index implemented**
     (`balance_index.py`): a per-address running `(credit, debit)` total in integer units (LMDB),
     `apply_rows`/`rollback_rows`/`rebuild_from_ledger`, turning the >200 s full-scan balance into an
     O(1) lookup. It **bit-matches `ledger_balance3` for every address** on real regnet data
     (`tests/test_balance_index.py`) — including the negative-height dev-reward / hypernode-payout
     "mirror" rows the node mints locally at commit (so the rebuild scans the WHOLE ledger, no height
     filter). Needs integer-amount storage to bit-match exactly. ◻ Remaining: `txid → location` +
     address-history indexes, and wiring the apply/rollback into the digester behind a flag. (When
     phase 5 retires the mirror-row hack, rewards apply from real columns instead.)
  3. **Integer atomic-unit amounts (phase 2) enabled** — compact exact integers instead of the current
     `REAL` floats / `'%.8f'` strings. Already replay-proven; currently default-off.
  4. **Bounded node footprint via a formalised pruning model (phase 5)** so a node need not retain the
     entire body history.
  Staged, config-flagged, default-off, each step independently replay-validated. SQLite stays for the
  queryable indexes; LMDB takes the heavy immutable bodies — **fully scalable without touching
  consensus.**
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
  **`node.py` is down from 2200 to ~1530 lines (−30%).** The remaining
  bulk is the ~1080-line socket-command `handle()`: its branches use `break`/`continue` against the
  connection loop and interleave consensus-critical sync, so turning it into a dispatch table needs a
  two-node test harness first (tracked above under "API-based sync"), not a blind rewrite. Each step
  keeps the flat-import layout working and the suite green.

## How we work

- Branch behaviour behind a config flag; keep it default-off until regnet + replay validate it.
- Never merge a change whose replayed block hashes differ.
- Test every change end to end — the suite launches a real regnet node; new behaviour ships with tests.
- Prefer surgical edits and small clean modules over sweeping abstract rewrites.
