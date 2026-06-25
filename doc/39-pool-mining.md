# doc/39 — Mining pool (Optipoolware), vendored + hf2-ready + modernized

Status: **landed** (`pool/`). The Optipoolware share pool (`bismuthfoundation/Optipoolware`) is vendored
into this repo so it lives in lockstep with the node, made fork-aware, cleaned up, and its web stack
modernized. **Consensus-critical PoW changes mirror the node's already-tested code; the full
fork-transition still needs live validation on a real pool + miners (see §5).**

## 1. Layout (`pool/`)

| File | Role |
|---|---|
| `optipoolware.py` | the pool **server** — a stdlib `http.server` (port 8525): `GET /work` + `POST /share` to miners; builds blocks; submits to the node over REST; hourly PPLNS payouts. |
| `optihash/optihash.py` | the bundled **CPU miner** miners run (gets work, hashes, submits shares). |
| `optiexplorer.py` | the **web dashboard** (port 9080) — now pure stdlib `http.server`. |
| `templates/index.html` | the dashboard UI (self-contained dark page, no CDN/Jinja). |
| `pool.toml` / `optihash/miner.toml` | pool / miner config (stdlib `tomllib`, typed; replaced the old `.txt`). |

It imports the node's modules from the repo root (`connections`, `mining_heavy3`, `options`, `essentials`,
`fork`, `vm_engine`) — so it uses the node's **already-hf2-ready** `mining_heavy3` directly.

## 2. hf2 readiness (Stage 1 — node-mirrored)

The PoW modernization (sha224 → blake2b) and the readiness signal are **bundled into the single hf2
fork** (doc/18-D). The pool + miner now mirror the node's `miner.py` / `mining_heavy3.py`:

- **Dual-algo Heavy3.** The miner's hot pre-filter and the authoritative `mining_heavy3.diffme_heavy3`
  both switch the inner hash to `blake2b(digest_size=28)` when `new_pow` is set (else sha224). `new_pow`
  is passed end to end; the 1 GB anneal + substring difficulty are unchanged.
- **`new_pow` source.** The pool's `_refresh_fork_state()` reads the node REST `/api/fork` each block and
  sets `new_pow = (tip+1) >= fork_height`, exactly like `miner._new_pow`.
- **Coinbase signal coupling.** The coinbase openfield is `cb_prefix + nonce`, where `cb_prefix` =
  `fork.FORK2_SIGNAL` (`"hf2"`) when signalling, plus the **VM pre-state root** post-fork
  (`vm_engine.embed_state_root`, fetched from `/api/vm/contracts`). The miner mines over
  `address + (cb_prefix+seed+nonce) + blockhash` — byte-identical to the node's PoW input — and submits
  that openfield, so the node both validates the PoW and detects the signal.
- **getwork is append-only.** The work tuple gains `[4]=cb_prefix [5]=new_pow`; an un-upgraded miner that
  reads only `[0:4]` still works.

**Signalling** is controlled by the node's own `fork_signal` config flag (the pool reads
`config.fork_signal`); set it on the pool's node to vote the fork in. **Until activation,
`new_pow=False`/`cb_prefix=""`, so the pool behaves exactly as pre-hf2 — zero change on mainnet today.**

## 3. Cleanup (Stage 2)

`optihash.py`: removed dead `diffme()` + `bin_convert_orig()` (a post-fork wrong-algo footgun); fixed the
`str.strip(charset)` config-parse bug (→ `split('=',1)`); fixed the worker that re-raised and deadlocked
`runit()`'s `hq.get` (now bounded), and the join loop that joined only the last process; added a sys.path
bootstrap so the in-repo miner uses the modernized root `connections`/`mining_heavy3`; dropped the stale
`optihash/connections.py` duplicate (wire-identical to the root copy).

## 4. Web modernization (Stages 3–4)

`optiexplorer.py` dropped **Flask + Tornado** for pure stdlib `http.server` (mirrors the node's
`rest_api.py`): `GET /` serves a dark, dependency-free, auto-refreshing dashboard; `GET /api/stats`
returns `{ network, pool, miners, payouts, pending }`. It **no longer scans `static/ledger.db`** — network
height/difficulty, hf2 fork status, and the pool's rewards/payouts come from the node REST
(`/api/status`, `/api/difficulty`, `/api/fork`, `/api/address/<pool>/transactions`), cached 20 s; per-miner
state still comes from `shares.db`. The dashboard shows a sha224↔blake2b PoW badge and a
pre-fork/locked-in/active HF2 card. Deps pruned (no flask/tornado) — Stage 5.

## 5. What's verified vs needs live validation

**Verified here (headless, no node/ledger):** `py_compile` of all changed files; a PoW **byte-identity**
cross-check (miner pre-filter input == `diffme_heavy3` input == node `miner.py` PoW input; `hf2` signal
detected; pre-fork path unchanged); and an HTTP smoke test of the Flask-free explorer (`/` → 200 HTML,
`/api/stats` → 200 JSON, bad route → 404).

**Needs live validation on a real pool + miners across the activation height** (cannot be done in this
environment — no node+miners+fork): the end-to-end blake2b transition (miner finds a blake2b share → pool
validates `new_pow=True` → builds a signalling coinbase → node accepts); the **VM state-root commitment**
the node enforces post-activation; and the exact activation-boundary `(tip+1) >= fork_height` off-by-one.
Deploy Stage 1 and confirm against a synced node **before** the fork.

## 6. Node communication: 100% REST (no legacy socket to the node)

The pool now talks to the node **entirely over the REST API** — no `connections` socket calls to the node
remain (via the `_node_get`/`_node_post` helpers). Off the old socket commands:

| was (socket) | now (REST) |
|---|---|
| `blocklast` / `diffget` (worker) | `GET /api/status` + `/api/difficulty` |
| `api_mempool` (coinbase build) | `GET /api/mempool` |
| `mpinsert` (payouts) | `POST /api/transaction` |
| fork state | `GET /api/fork` (+ `/api/vm/contracts`) |
| `block` (submit mined block) | **`POST /api/block`** (new) |

**`POST /api/block`** (node, `rest_api.py`) is the REST transport for the legacy socket `block` command:
it routes the submitted block through the **identical `digest_block` path** (no new consensus logic) with
the same mainnet guards (allowed/whitelist, ≥5 connections, not mid-digest, synced), gated by
`rest_api_write` so it is **inert on prod by default**. The pool POSTs the found block to its local node
(which propagates it); the old socket peer-broadcast stays only as a fallback when REST is unavailable.
Run the node with `rest_api=True` + `rest_api_write=True`. *(Verified live on regnet: 403 without
rest_api_write, 400 on a missing block, malformed block routes to `digest_block` → graceful
`accepted: false`.)*

**Invariants preserved:** the coinbase reward tx tuple shape, the 9-field share tuple, and the digest path
are unchanged. The **miner↔pool protocol is now HTTP too**: the pool runs a stdlib `http.server`
(`GET /work` → the work package; `POST /share` → a found nonce, JSON both ways) and the bundled miner
uses `urllib` — no `socketserver`, no `socks`, no `connections` on the miner side. The **only** socket
call left anywhere in the pool is the optional block-broadcast **fallback** used when the node REST is
unavailable. So the pool stack is now HTTP/REST end to end.

## 7. Running

```bash
# pool server (needs a running node + the pool's privkey.der/pubkey.der + pool.toml)
cd pool && python3 optipoolware.py
# web dashboard (reads the node REST; default node rest_api_port 5659)
cd pool && python3 optiexplorer.py        # http://0.0.0.0:9080
# a miner (edit optihash/miner.toml: pool ip/port, miner_address, threads)
cd pool/optihash && python3 optihash.py
```
