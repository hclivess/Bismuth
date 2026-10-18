# Bismuth Node — Modernization Release

A large, **behavior-preserving** modernization of the Bismuth node. The guiding rule throughout:
**consensus does not change except at an explicit, signalled hard fork.** Everything that would alter a
block hash is built, replay-validated on regnet, and ships **inert** — it does nothing on mainnet until
the chain itself votes it in. So this release is safe to run today: the operator-facing wins are live now,
and the consensus-touching work waits behind the fork it was designed for.

> **Legend:** ✅ **active now** · 🕒 **gated** — present + tested, switches on only at a signalled fork.

---

## What's new in 4.7.0 — LMDB-only ledger

The node no longer uses SQLite anywhere. Consensus is unchanged: same blocks, same hashes, same validation.

- **One LMDB ledger** ✅ — `static/blockstore-ledger.db` holds the blocks, the per-block difficulty and
  every index (signature, address history, integer-unit balances, content txid, pubkey registry). A block
  and all of its index entries commit in ONE transaction; a reorg rolls back in one. `ledger.db`,
  `hyper.db`, `index.db`, `mempool.db`, hyperblocks, RAM mode, the commit-marker lockstep, reconcile,
  sequencing checks and autoheal are gone, with ~11k lines of code.
- **In-memory mempool** ✅ — pending transactions live in memory (they are re-broadcast by peers after a
  restart); admission rules unchanged.
- **Dev / Hypernode reward mirrors removed** ✅ — the negative-height synthetic rows are gone; their
  concluded totals (Dev 3,151,476.52720086 BIS, HN 6,521,167.33333333 BIS) are fixed ledger allocations
  (`ledger_genesis.py`), applied automatically, so both balances match the network.
- **Tokens & aliases** ✅ — served only by the always-on `tokens_aliases` plugin (its own LMDB store). The
  first start after upgrading builds that index once from the ledger (~15 minutes on mainnet; the node
  starts syncing when it is done).
- **No self-connections** ✅ — the node never dials any address of its own machine (public IP included),
  so a peer list gossiped back to us can no longer make the node "agree" with itself at 100% consensus.
- **Installer** ✅ — `sudo ./install_node.sh` is the documented one-command install; it now also checks an
  existing ledger's schema and prints the upgrade command.

**Upgrading an existing node** (block store from 4.6.x):

```bash
sudo systemctl stop bismuth-node
git pull && git checkout v4.7.0.0
python3 scripts/upgrade_ledger.py static/blockstore-ledger.db --anchor http://<a synced REST node>:5659
sudo systemctl start bismuth-node
```

The upgrade replays the difficulty history (anchored on the peer's published values) and rebuilds every
index from the blocks — ~10 minutes on mainnet, no download. The old SQLite files can be deleted
afterwards. A node with no ledger needs nothing: it bootstraps from a peer snapshot.

## What's new in 4.6.0

Continuation of the modernization, same rule (consensus only changes at a signalled fork). New since the
4.5.0.x series — see `doc/CHANGELOG-2026-06.md` for the commit-anchored detail:

- **One-command node onboarding** ✅ — `git clone … && sudo ./install_node.sh`: all deps, the
  `bismuth-node` service, and an **auto-bootstrapped ledger** on first run (no manual download). The
  website "Run a node" page now reflects this.
- **TOML configuration** ✅ — `config.toml` via stdlib `tomllib` (zero new dep), fully backward-compatible
  with `config.txt`; non-destructive `scripts/migrate_config.py`. (doc/11)
- **Modern Tor / onion routing** ✅ — tri-state `tor` (off/external/**managed**): the node can launch and
  own its own tor with an ephemeral v3 hidden service; `install_node.sh --tor` bundles it. Default off /
  clearnet-unaffected. (doc/38)
- **Faster restarts & lighter queries** ✅ — fixed the multi-minute sync-resume (bounded peer dial +
  bounded startup difficulty scan); every heavy read is now an ordered LMDB index range scan.
- **The ledger is one LMDB environment** ✅ — blocks, per-block difficulty and every index (signature,
  address history, balances in integer units, content txid, pubkey registry) in `blockstore-<ledger>`,
  one transaction per block commit and per reorg. The relational ledger files, hyperblocks, the index
  and mempool databases and the negative-height reward rows are gone (the concluded Dev/HN totals are
  fixed allocations). Existing block stores upgrade with `scripts/upgrade_ledger.py`. (doc/05, 26, 36)
- **Difficulty-divergence detector + guarded self-heal** ✅ — recurrence-prevention for the difficulty
  corruption class, with a permanent restart-loop guard. (doc/35)

---

## Highlights

- **Run the node as a systemd service** — no more `screen`/`nohup`.
- **Graceful shutdown** that keeps the ledger consistent, plus **self-healing** startup recovery.
- A **real solo miner** in-tree (mempool txs + Heavy3 PoW), and a **dual-algorithm Heavy3** ready to switch on by signal.
- A **post-quantum signature suite** (ML-DSA / FIPS 204) added to the pluggable signer layer — real, tested code.
- An on-chain-signalled **auto hard-fork** mechanism so the bundled consensus upgrades activate without a chain split.

---

## Operations ✅

- **systemd service installer** (`scripts/install-node-service.sh`). One command installs `bismuth-node`:
  auto-detects repo/python/user, gracefully stops any running node, enables + starts it, survives reboots,
  restarts on failure. `systemctl stop/restart` is graceful; `journalctl -u bismuth-node -f` for logs.
- **Graceful shutdown.** `SIGTERM`/`SIGINT` (and the `stop` command) now finish the in-flight block and
  drain `db_lock` before exit. Even an abrupt kill leaves a clean ledger: each block is one LMDB
  transaction.
- **Reputation-weighted consensus.** Peers earn/lose reputation on whether the heights they advertise turn
  out real; the "most common block" tip rule is weighted by reputation and gates deep rollbacks (anti-sybil).
- **Connectivity fixes.** No longer self-dials `127.0.0.1` and reports false 100% consensus; saner peer
  retry backoff.

## Mining

- **Built-in solo miner** ✅ (`miner.py`, opt-in `mine=True`). Assembles a full block — pending mempool
  transactions + a signed coinbase carrying the fork-readiness signals — mines real Heavy3, and digests it.
  The in-tree block-builder the GPU nonce-finders (`gpuminer/`) feed into.
- **Dual-algorithm Heavy3** 🕒. The inner hash can modernize `sha224 → blake2b` (same anneal, same
  difficulty); miner and validator switch together at the **single signalled `hf2` fork** (the interim
  separate `pow2` signal was folded into hf2 on 2026-06-12 — stamping `hf2` asserts blake2b readiness,
  and GPU kernels must swap in lockstep at the activation height). Built and regnet-tested; off until
  miners signal. Stale `pow2` keys in old regnet lock-in sidecars are ignored; wipe regnet datadirs
  that carried split (hf2≠pow2) lockins before replaying them. The transition is gated by
  `tests/fork_transition_smoke.py` (lock-in → mid-transition restart → boundary crossing → reorg
  across the boundary → lost-sidecar self-healing), and fork-height detection now runs BEFORE each
  block is judged, so a lost sidecar can never wedge a node on era-mismatched PoW.

## Cryptography — a signature menu ✅ (consensus acceptance 🕒)

- The pluggable `polysign` layer gains a **post-quantum family**: **ML-DSA-44 / 65 / 87** (FIPS 204 /
  CRYSTALS-Dilithium, NIST Cat 2/3/5) and **secp256r1 / P-256** (passkeys / hardware), alongside the
  existing RSA / ECDSA / ed25519. Wallet key is a 32-byte seed; address is a hash of the public key.
- These are **real, tested signers** (sign/verify, deterministic keys, tamper rejection) and are
  registered in the factory — they just aren't accepted in consensus until a future `pq` fork. The
  pivot, if a quantum timeline ever becomes credible, is "flip the fork," not "write the crypto."

## Consensus & the hard fork 🕒

- **Auto hard-fork (`hf2`).** Upgraded miners stamp a coinbase signal; every node computes the same
  activation height from the chain (no split); rules switch on at a buried round boundary. `/api/fork`
  reports readiness.
- Bundled behind it: **LWMA difficulty** (fixes the up-only ratchet), **dynamic base fee** (algorithmic,
  from recent block fullness), integer/binary serialization + content-hash txid + canonical sig encoding.
  Old blocks keep their exact bytes and hashes; new rules apply forward only; no re-sync.

## Storage 🕒 / Edges ✅

- **Storage (active):** one LMDB ledger — pubkey-deduped blocks, the difficulty record and O(1)
  balance / signature / address / txid indexes committed atomically per block; integer atomic-unit
  amounts (opt-in). Replay-verified byte-identical block hashes.
- **Edges (live):** parallel compressed **REST API** (now **on by default**), the **block explorer**
  (blocks / tx / address / tokens / nodes / supply), Bitcoin-JSON-RPC and Ethereum/ERC compatibility
  shims (opt-in), and a zero-downtime **bootstrap snapshot**.
- **The whole chain in LMDB** — mainnet's 4,937,451 blocks / 6,426,832 txs are **9.7 GB** in the LMDB
  ledger, less than half the previous on-disk ledger (−58%), byte-verified. The saving is pubkey dedup —
  the entire chain has only **5,586 distinct public keys** across 6.4M txs. Stores built before the
  ledger became LMDB-only are brought to the current schema with `scripts/upgrade_ledger.py` (difficulty
  history replayed and anchored on peers' published values, then every index rebuilt); the node names
  the script and refuses to start on an old schema.
- **`/api/snapshot` is on-demand and DB-direct** — generated from the live LMDB ledger at request time via
  `env.copyfd` (consistent, compacted, streamed straight to the socket): **no pre-built tarball, no doubled
  ledger on disk, never stale**. Measured: **9.7 GB in 75 s**, and the bytes open as a valid block store with
  a tip hash matching the advertised header. `snapshot_serve` + `bootstrap_p2p` are now **on by default**, so
  fresh nodes bootstrap from **peers** (else seed genesis and sync) rather than one hosted file.
- **LMDB stores are namespaced per ledger** (`blockstore-ledger.db` vs `blockstore-regmode.db`).
  They previously shared one directory across mainnet/testnet/regnet, so a regnet run could hand its blocks
  to a mainnet node and the test suite's startup wipe destroyed a live node's stores.

## Documentation ✅

- A complete `doc/` suite (`00`–`21` + README), **audited against the code** this cycle — including new
  deep-dives on the hard fork (`18`), post-quantum (`20`), and mining (`21`).

---

## Upgrade notes

- **Recommended:** `sudo bash scripts/install-node-service.sh` to move off `screen`/`nohup`.
- **Required deps:** `ed25519` + `coincurve` (mainnet carries those tx types — a node without them silently
  rejects every such block). **Optional:** `dilithium-py` (ML-DSA) and `cryptography` (P-256), lazy-loaded.
- **No flag day.** Nothing consensus-affecting turns on until the signalled `hf2` fork — ONE fork that
  bundles everything, including the blake2b PoW swap. Running this release changes how you *operate* the
  node, not how it *validates* the chain — until the network deliberately votes the fork in.
