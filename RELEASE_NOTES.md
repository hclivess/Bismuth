# Bismuth Node — Modernization Release

A large, **behavior-preserving** modernization of the Bismuth node. The guiding rule throughout:
**consensus does not change except at an explicit, signalled hard fork.** Everything that would alter a
block hash is built, replay-validated on regnet, and ships **inert** — it does nothing on mainnet until
the chain itself votes it in. So this release is safe to run today: the operator-facing wins are live now,
and the consensus-touching work waits behind the fork it was designed for.

> **Legend:** ✅ **active now** · 🕒 **gated** — present + tested, switches on only at a signalled fork.

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
  drain `db_lock` before exit, so `ledger.db` and `hyper.db` stay at the same height.
- **Non-destructive cross-integrity recovery.** If the two DBs ever diverge (e.g. an abrupt kill), startup
  now keeps the common height and trims only the excess, then resyncs — instead of rolling back further
  than necessary. Turns a "stuck, flapping, won't sync" node into an automatic ~20s recovery.
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

## Smart contracts (decentralized apps) 🕒

- **A single RISC-V (RV32I) execution engine** (`bismuth_riscv` + `vm_engine` + `vm_state`): deploy/call
  contracts, LMDB-backed code+storage+balances.
- **Real value custody** — contracts hold and move actual BIS (deposit/withdraw via a custody sink),
  rollback-deterministic by re-execution.
- **Enforced state root** — the VM state root is committed in the coinbase and consensus-checked (mandatory
  post-fork). Includes a complete HTLC example. All POST-FORK + flag-gated.

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

- **Storage (shadow/validated):** LMDB content-addressed block store, O(1) balance index, integer
  atomic-unit amounts, reward sidechain — each proven lossless / replay-byte-identical.
- **Edges (live):** parallel compressed **REST API**, the **block explorer** (blocks / tx / address /
  tokens / nodes / supply / contracts), Bitcoin-JSON-RPC and Ethereum/ERC compatibility shims (opt-in),
  and a zero-downtime **bootstrap snapshot**.

## Documentation ✅

- A complete `doc/` suite (`00`–`21` + README), **audited against the code** this cycle — including new
  deep-dives on the hard fork (`18`), the VM (`19`), post-quantum (`20`), and mining (`21`).

---

## Upgrade notes

- **Recommended:** `sudo bash scripts/install-node-service.sh` to move off `screen`/`nohup`.
- **Required deps:** `ed25519` + `coincurve` (mainnet carries those tx types — a node without them silently
  rejects every such block). **Optional:** `dilithium-py` (ML-DSA) and `cryptography` (P-256), lazy-loaded.
- **No flag day.** Nothing consensus-affecting turns on until the signalled `hf2` fork — ONE fork that
  bundles everything, including the blake2b PoW swap. Running this release changes how you *operate* the
  node, not how it *validates* the chain — until the network deliberately votes the fork in.
