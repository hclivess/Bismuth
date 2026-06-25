# Optipoolware — Bismuth mining pool

A reference share-mining pool for [Bismuth](https://github.com/hclivess/Bismuth), vendored into the node
repo so it stays in lockstep with consensus. It is **hf2-ready** (the Heavy3 PoW modernises sha224 →
blake2b at the fork, mirroring the node) and modernized (pure-stdlib web dashboard, no Flask/Tornado).

Three parts:

| Component | What it does | Default port |
|---|---|---|
| `optipoolware.py` | the **pool server** — hands miners work, validates shares, builds + broadcasts blocks, pays miners (PPLNS, hourly) | 8525 |
| `optihash/optihash.py` | the **CPU miner** miners run against the pool | — |
| `optiexplorer.py` | the **web dashboard** — live pool/network stats | 9080 |

> Deep design, the hf2 PoW flow, the preserved wire invariants, and exactly what is verified vs. needs
> live validation across the fork: **[`doc/39-pool-mining.md`](../doc/39-pool-mining.md)**.

## Quick start

A running Bismuth `node.py` is required (the pool relays blocks/txs to it and reads its REST API). Run
the pool components **from the repo root** so they resolve the node's modules (`connections`,
`mining_heavy3`, `options`, `essentials`, `fork`):

```bash
pip install -r pool/requirements.txt          # pycryptodome, simple-crypt, PySocks, requests (no flask/tornado)
python3 pool/optipoolware.py                   # pool server on :8525  (Ctrl-C stops it cleanly)
python3 pool/optiexplorer.py                   # dashboard on http://0.0.0.0:9080
```

The pool needs its payout keypair (`privkey.der` / `pubkey.der`) and `pool.txt` in its working dir.

Miners run `optihash` (no node needed — just a Bismuth address):

```bash
cd pool/optihash && python3 optihash.py        # reads miner.txt
```

## hf2 readiness

The PoW swap (sha224 → blake2b) is bundled into the single hf2 fork. The pool reads the node's REST
`/api/fork` each block and, post-activation, validates shares with blake2b and stamps the `hf2` readiness
signal (+ the VM state root) into the coinbase — mirroring the node's `miner.py` / `mining_heavy3`.

- **Signalling** is driven by the node's own `fork_signal` config flag (set it on the pool's node to vote
  the fork in).
- Until activation the pool behaves **exactly as pre-hf2** (no change today).
- ⚠️ The blake2b switch mirrors the node's tested code, but the **end-to-end fork transition** (incl. the
  post-activation VM-state-root commitment and the exact boundary) **needs live validation on a real pool
  + miners before the fork** — see `doc/39`.

## Configuration

### `pool.txt` (pool server + dashboard)

| Key | Meaning |
|---|---|
| `mine_diff` | pool share difficulty (static) |
| `min_payout` | minimum balance (BIS) before the hourly autopayout pays an address |
| `pool_fee` | pool fee, percent (e.g. `1` = 1%) |
| `alt_fee` | fee to an alternate address (dev/charity/…), percent |
| `alt_add` | the alternate address; also the fallback share address so a miner sending a bad address isn't wasted |
| `worker_time` | how often (seconds) the pool refreshes blockhash/diff/fork state from the node |
| `m_timeout` | minutes without a share before a worker's hashrate is treated as 0 |

`fork_signal` comes from the **node's** config (`config.txt`), shared via the node's `options`.

### `optihash/miner.txt` (miner)

| Key | Meaning |
|---|---|
| `mining_ip` / `port` | the pool's address / port |
| `mining_threads` | number of mining processes |
| `miner_address` | the miner's Bismuth address (rewards go here) |
| `nonce_time` | seconds to mine before fetching fresh work |
| `miner_name` | base worker name (the thread number is appended) |
| `hashcount` | nonce-array sizing per cycle (typical `20000`) |
| `tor` | `1` to route via a local Tor SOCKS proxy |

## How it works

1. The miner asks the pool for work: `(blockhash, share-diff, pool address, net-diff, [hf2 prefix, new_pow])`.
2. It Heavy3-hashes `pool_address + openfield + blockhash` (sha224 pre-fork / blake2b post-fork) until a
   nonce meets the share difficulty, then submits it to the pool.
3. The pool validates the share against the node's `mining_heavy3` (same algo the node consensus uses); if
   it meets the **network** difficulty it pulls the mempool, builds + RSA-signs the coinbase, and
   broadcasts the block to its peers.
4. Shares are recorded in `shares.db`; the autopayout thread pays out hourly per PPLNS, minus fees.

## Files

- **Run from the repo root:** `pool/optipoolware.py`, `pool/optiexplorer.py`, `pool/pool.txt` (+ the
  pool's `privkey.der`/`pubkey.der`).
- **Miner:** `pool/optihash/optihash.py` + `miner.txt`. In-repo it imports the node's modernized
  `connections` + `mining_heavy3` via a sys.path bootstrap; packaged standalone, ship those two modules
  (and the Heavy3 binary) alongside it.

Standalone executables for Windows / Linux / macOS are produced by the repo's release CI and attached to
each GitHub release.
