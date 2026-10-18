# 17 — Roadmap

> The plan for modernizing the Bismuth node. Storage details: [`05-database-and-ledger.md`](05-database-and-ledger.md)
> (the LMDB ledger) and [`26-storage-postfork.md`](26-storage-postfork.md) (the migration record).

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
- **Ledger schema versioning.** The LMDB ledger records its schema in `meta`; the node refuses an older
  schema and names the upgrade (`scripts/upgrade_ledger.py`: difficulty replay anchored on peers, then
  `BlockStore.reindex`).
- **Integer atomic-unit storage (phase 2).** `amounts.py` provides the exact decimal↔integer-units
  converter; `replay_verify.py` proves integer round-tripping changes no block hash. The live cutover is **enabled on regnet** behind the
  `ledger_integer_amounts` flag (default **off**, so mainnet is untouched). Display/consensus edges all
  reconstruct the legacy decimal strings at the boundary.
- **O(1) balances (phase 4).** The ledger's `bal` sub-db keeps per-address credited / rewards / debited
  / fees in integer units, maintained inside each block commit and reversed by rollback; consensus, the
  mempool and every API read it.
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
- **Decentralized bootstrap.** `chain_ops.bootstrap` depends on no download host: a fresh node streams a
  DB-direct LMDB snapshot from a peer (`/api/snapshot`, `env.copyfd`), verifies its tip height + hash,
  and the digester re-validates forward; with no snapshot available it seeds genesis
  (`ledger_genesis`) and syncs (doc/43). Covered by `tests/test_bootstrap_local.py` and
  `tests/test_snapshot_db_direct.py`.
- **Legacy wire-compatibility verified live.** `legacy_sync_probe.py` (read-only) confirms this codebase
  still speaks the legacy socket protocol to the current mainnet: version handshake → `ok`, compatible
  peer versions (`mainnet0021/0022`), peer-list exchange (≈500 peers) and block-height negotiation all
  succeed against live peers (tip **4,845,284**). Note: legacy peers reject ancient checkpoints
  (`blocknf`/rollback), so a from-genesis forward sync is not served — a bootstrap snapshot is required,
  which is exactly what the peer bootstrap above provides.
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
  - ✅ **Live wiring DONE + two-node-proven.** `api_sync_worker` runs the catch-up loop behind the
    default-off `api_sync` flag (+ `api_sync_source` = a `host:rest_port` peer): it polls the peer tip,
    pulls validated segments via `api_sync.sync_segment`, and feeds them to the SAME `digest_block` the
    socket path uses (transport-only swap; consensus validation identical). The two-node harness now
    exists — `tests/test_two_node_api_sync.py` (regnet paths/port env-parameterized so N nodes co-exist):
    node B starts at genesis and reconstructs node A's entire chain **over REST alone**, block-for-block
    identical, ledger indexes included. So
    one node ingesting another's blocks over the API is validated end-to-end.
  - ◻ **Remaining: mainnet rollout.** **Reality check (measured on mainnet 2026-06):** no live peer is
    REST-capable yet (legacy peers expose only the socket port), so on mainnet this still only lets THIS
    node *serve* fast sync; live catch-up stays on the legacy socket protocol (≈0.8 blocks/s) until peers
    upgrade and `api_sync` is turned on against a REST-capable source.
- ✅ **Explicit reward model (phase 5).** The dev-fund + hypernode-payout rewards, once minted locally
  every 10th block as unsynced negative-height rows, concluded at block 4,380,000. Their totals are now
  fixed balance allocations (`ledger_genesis.MAINNET_ALLOCATIONS`, meta `alloc`) — no synthetic rows,
  both addresses' balances equal the network's, and every affected block is anchored by the block-hash
  checkpoints.

## 🗺️ Planned

- **Consensus hard fork (the SINGLE hf2 fork).** Change the consensus serialization itself to sign/hash
  **native integer units + a binary/struct tx encoding**, deleting the `'%.8f'`/`'%.2f'` string
  reconstructions (every site is tagged `# HARDFORK` in the code — `grep -rn "# HARDFORK"`). Adopt a
  bounded, content-derived **txid** (nado-style: `blake2b(tx_content)`, the signature signs the txid) to
  replace the ad-hoc `signature[:56]` slice. After the fork, storage/boundary/APIs are integer
  end-to-end. **This serialization rework folds into the ONE hf2 fork — not a second fork signal:** every
  gate reads `node.fork_height` (`fork.py`; the Heavy3 PoW modernisation and LWMA difficulty are bundled
  into the same `hf2` signal). The full byte-level spec is [`29-hf2-serialization-v2.md`](29-hf2-serialization-v2.md).
  - ✅ **Stage 0 — the v2 codec shipped (dormant).** `bismuth_serialize.signature_buffer_v2` /
    `tx_id_v2` (`bismuth_serialize.py:76-97`): the canonical little-endian BINARY tx pre-image over the
    same six content fields with **native integer units** (atomic) and an **integer centisecond
    timestamp** — no `'%.8f'`/`'%.2f'` reconstruction. Added BESIDE the frozen legacy functions (never
    mutated, characterization-locked); a `0xB2` magic byte can't alias a legacy pre-image. **Inert until
    a consensus site routes through a fork-aware dispatcher** (staged rollout, doc/29 §4).
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
    legacy signing** post-fork (shielded is staged behind `shielded_fork_height`, doc/22) (multisig:
    explicit pubkeys + N-of-M over the frozen buffer — it does **not** sign the txid). **ALL post-fork txs still receive the content-hash txid as their canonical
    id.** Pre-fork is byte-identical; historical txs keep their `signature[:56]` ids. Covered by
    `tests/test_hf2_recoverable.py` and `tests/test_hf2_fork_transition.py`.
  - **Signature & public-key storage optimization (a stated goal for the fork; spec'd in doc/29 §2-3).**
    The 1068-byte base64 RSA public key is carried and stored on **every** transaction, as is the
    684-byte base64 signature — together the dominant part of a block body. The fork should: (a) carry the public key by
    **reference** — a sender's address is the hash of its key, so after an address's first appearance the
    full key need never be repeated (a per-address key table); (b) store keys/signatures as **raw bytes**
    rather than base64 (~25 % smaller for RSA, and far smaller for the 32–33-byte ECDSA/ED25519 keys and
    64-byte sigs); (c) keep only the content-hash txid + signature on-chain. The same dedup can be done
    **pre-fork, locally** in the LMDB ledger (transparent, lossless — the measured 4.3 GB → 1.73 GB per 1M
    blocks); the fork makes it canonical and network-wide.
  - **Coinbase compaction (spec'd in doc/29 §2.4).** The coinbase (miner reward) is ingested as a *full*
    signed transaction — a 1068-byte RSA public key + 684-byte signature like any tx, plus (a Bismuth
    quirk) the PoW **nonce in its openfield** (which the hf2 readiness signal currently rides in).
    Wasteful: the reward is consensus-derived (needs no repeated key) and the nonce deserves its own
    field. The fork should give the coinbase a compact, purpose-built encoding (Bitcoin-style special
    coinbase).
    - ✅ **Free-form coinbase fields + mining header — IMPLEMENTED + regnet-validated (doc/41).** Post-fork
      the PoW nonce moves into the coinbase's **signature** slot and the `"vmsr"<root>` + optional `"hf2"`
      signal into its **public_key** slot (both freed by compaction), so `operation`/`openfield` become
      **optional, uncapped, free-form** miner data — nothing is enforced on them. The coinbase stays
      PoW-authorized and is never signature-verified (`recipient == address` still enforced); the v2
      pre-image gains an omit-when-empty flag (version `0x01`→`0x02`). See
      [`41-hf2-coinbase-free-fields.md`](41-hf2-coinbase-free-fields.md).

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
  - **Bitcoin-compatible JSON-RPC** (`rpc_bitcoin.py`, flag `rpc_bitcoin`, port 8332) — **SHIPPED** as a
    maximal faithful adapter: ~32 `bitcoind` methods — chain/header/mempool/mining/network reads
    (`getblockcount`/`getblock`/`getblockheader`/`getchaintips`/`getrawmempool`/`getmininginfo`/…), balance
    + tx reads (`getbalance`/`getrawtransaction`/`gettransaction`/`estimatesmartfee`/`validateaddress`), and
    the gated write paths (`sendrawtransaction`/`submitblock`). Enough for most exchange/explorer
    integrations. **Post-hf2 native:** ids are content-hash txids, reads route through `block_store` (LMDB),
    never an unbounded ledger scan. Methods with no Bismuth backing (the UTXO/Script/PSBT/server-wallet
    families — `gettxout`/`listunspent`/`getnewaddress`/`sendtoaddress`/…) return an honest `-32601` with
    the specific reason rather than pretending.
  - **Ethereum/ERC-compatible interface** (`rpc_ethereum.py`, flag `rpc_ethereum`, port 8545) — **SHIPPED**:
    42 `eth_*`/`net_*`/`web3_*`/`txpool_*` methods. NOT read-only — `eth_call`/`eth_getCode`/
    `eth_getStorageAt`/`eth_estimateGas` execute over the committed **RISC-V `vm_state`** (the post-hf2
    on-chain VM below — the Ethereum *execution model*, not EVM bytecode). Honestly bounded by *literal*
    Ethereum compatibility, a deliberate design divergence plus roadmap items — NOT "Bismuth can't do
    contracts": addresses are 28-byte (no reversible 0x map), replay is content-txid dedup (no account
    nonce), and there is no contract VM at all — so `eth_call`/`eth_getCode`/`eth_getStorageAt`/
    `eth_getLogs` have nothing to read. Unbacked methods return `-32601` with an honest reason. So it is
    **not** a MetaMask drop-in, but it is a faithful Bismuth-flavoured `eth_*` API over chain data. Both adapters are unauthenticated + localhost-only (write paths
    additionally gated by `rest_api_write`); expose via a reverse proxy, never straight to the internet.
- **Decentralized-apps v2 protocol — on-chain VM execution. REMOVED, not planned.** A RISC-V (RV32I)
  contract VM with gas metering, a committed state root, value custody and a `vm:deploy`/`vm:call` tx
  format was built and regnet-tested behind its own activation gate, then **deleted in full** — it never
  activated on any live network and is not on the roadmap. `vm:` operations carry no meaning: they are
  stored as ordinary inert transaction data.

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
  post-fork — the RV32I VM (and, if later scheduled, shielded ring-sig verification, doc/22)) are
  **ported with parity tests, never reinterpreted**. Default-off and non-authoritative until it matches Python block-for-block.
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
- ✅ **Scalable storage — the LMDB ledger (phase 7, complete).** Measured on the live mainnet ledger
  (22 GB, block 4.85M, 7.13M txs) the relational per-address balance aggregate was a full scan that did
  not finish within 200 s — the scalability wall. The ledger is now ONE LMDB environment
  (`block_store.py`, doc/05): height-keyed block bodies with public-key dedup (each distinct key stored
  once, keyed by blake2b since a 1068-byte key exceeds LMDB's 511-byte key limit), the per-block
  difficulty record, and maintained indexes — signature, address history, per-address balances in
  integer units, content txid, pubkey registry — all written by `commit_block` in one transaction and
  reversed by `rollback` in one transaction. Mainnet's whole chain is **9.7 GB** (−58%; only 5,586
  distinct public keys across 6.4M txs), byte-verified, every block hash replay-identical. The balance is
  an O(1) read; the negative-height reward rows are replaced by fixed allocations
  (`ledger_genesis.MAINNET_ALLOCATIONS`). Chosen **LMDB over RocksDB:** a single mmap'd file, no
  background compaction, copy-on-write MVCC reads alongside a single writer — a near-perfect fit for an
  append-heavy, read-heavy, immutable workload, and a trivial embedded dependency
  (`pip install lmdb msgpack`). Older stores upgrade with `scripts/upgrade_ledger.py`. Remaining in this
  area: a formalised pruning model so a node need not retain the entire body history.
- **Repository reorganization & modularization.** Retire dead files (✅ done — moved to `attic/` by
  import-graph analysis) and break up the over-long modules behind **behavior-preserving, test-green**
  extractions. `node.py` (~2.2k lines, with a ~1080-line socket-command `handle()`) is the prime
  target: turn the `if/elif` command chain into a dispatch table of small handlers, and lift the
  bootup/init and ledger-maintenance functions into focused modules (most already take `node`
  explicitly, so they extract by dependency injection). Cuts done so far: the 837-line `apihandler.py`
  god-class split into domain mixins (`apihandler_blocks`/`_address`/`_tx`) recombined via
  `class ApiHandler(BlockApiMixin, …)`, leaving a 115-line dispatcher core (its pure block→JSON
  formatters had already moved to `block_format.py`); `dbhandler.py` reduced to a ~180-line facade over
  the LMDB ledger and `mempool.py` to an in-memory pool once the relational ledger was retired; the
  chain-maintenance cluster (`rollback`, `blocknf`, plus `bootstrap` / `seed_genesis`) lifted out of
  `node.py` into `chain_ops.py` by DI, with `blocknf` re-exported so `worker.py` is unaffected; the
  mempool-aware `balanceget` moved to `balances.py`; and the bootstrap/init helpers (`setup_net_type`,
  `open_ledger`, `node_block_init`, `load_keys`) lifted into `node_init.py` by DI (the consensus chain
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
