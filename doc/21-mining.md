# 21 — Mining (Heavy3 PoW, the solo miner, and the dual-algo fork)

Bismuth's proof-of-work is **Heavy3**: `hash(miner_address + nonce + block_hash)` → a memory-hard
**anneal** (7 random-access XOR lookups into a 1 GB `heavy3a.bin` "junction noise" file) → the difficulty
is the longest prefix of the block hash's bits that appears as a *substring* of the annealed result. The
1 GB table is what limits per-GPU advantage. Validation and mining share one function,
`mining_heavy3.diffme_heavy3`.

## Who builds a block

A block = the pending mempool transactions + a **coinbase** (the miner's reward tx) whose **openfield**
carries the mined nonce. Bismuth blocks are built by the entity that mines them:

| Component | File | Role |
|---|---|---|
| PoW (hash + anneal + difficulty) | `mining_heavy3.py` | shared by the node (validate) and miners (mine) |
| **Solo miner / block-builder** | **`miner.py`** | assembles a full block (mempool txs + signed coinbase), mines Heavy3, digests it |
| GPU nonce-finders | `gpuminer/` (CUDA `bis.cu`, OpenCL `bismuth.cl`) | brute-force nonces in parallel; pool-fed |
| Validator | `digest.py` → `mining_heavy3.check_block` | rejects a block whose PoW doesn't meet difficulty |

`gpuminer/` miners are **pool** miners — they `getwork` and submit solved nonces; the pool builds the
block. `miner.py` is the in-tree **block-builder/orchestrator** (a working CPU solo miner, and the
reference a pool patch follows).

## The solo miner (`miner.py`)

Enable with `mine=True` and a funded wallet; it runs in its own thread (`mining_loop`), serialised with
sync via `db_lock`. Each block:

1. `mine_nonce` — search a winning coinbase openfield on the current tip (no lock; pure compute). The
   openfield = a **fixed prefix** (the readiness signals + post-fork the VM state root) + the mined nonce.
2. acquire `db_lock`, **re-check the tip** (discard if it moved while mining),
3. `_build_block` — `[pending mempool txs…, signed coinbase]` in the exact tuple shape the digester wants,
4. `digest_block` — validate + commit (it manages `db_lock` release).

The coinbase openfield prefix (`_coinbase_prefix`):
- `hf2` — hard-fork readiness, if `fork_signal=True` (doc/18).
- `pow2` — modernised-PoW readiness, if `pow_fork_signal=True` (this doc, below).
- `vmsr`+root — the committed VM state root, **mandatory** once hf2 is active (doc/19).

The node detections **search** for these markers, so the concatenation order is not fragile, and they all
ride inside the PoW-hashed nonce, so PoW still validates over the whole thing.

## Dual-algo PoW — the modernisation fork (doc/18-D)

The single most security-sensitive change is a PoW transition, so it gets its **own** signalled fork,
separate from hf2. `diffme_heavy3(address, nonce, block_hash, new_pow=…)` selects the inner hash:

- `new_pow=False` → **sha224** (today's Heavy3),
- `new_pow=True` → **blake2b** (28-byte digest, same 224-bit width).

Everything else — the 1 GB memory-hard anneal and the substring-prefix difficulty — is **unchanged**, so
a node/miner switches purely on `new_pow`.

Activation is by on-chain signalling, **identical machinery to hf2** (`fork.dynamic_fork_height` is
signal-agnostic), with a different marker:
- miners stamp `pow2` (`fork.FORK_POW_SIGNAL`) into the coinbase once they can mine the modernised PoW;
- when the trailing window is all-signalled it locks in and activates at a buried boundary;
- the digester caches `node.pow_fork_height`; `block_height >= node.pow_fork_height` ⇒ `new_pow=True`,
  fed to `check_block` (validation) and used by `miner.py` (mining).

So the miner runs old→new across the fork automatically: below the height it mines/validates sha224, at
and above it mines/validates blake2b. The GPU kernels (`bis.cu` / `bismuth.cl`) must swap sha224→blake2b
in lockstep at that height — they implement the same `diffme_heavy3`.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `mine` | `False` | run the built-in solo miner (`miner.py`) |
| `fork_signal` | `False` | stamp the `hf2` signal into mined coinbases |
| `pow_fork_signal` | `False` | stamp the `pow2` (modernised-PoW) signal into mined coinbases |
| `heavy` / `heavy3_path` | — | require/locate the 1 GB `heavy3a.bin` (generated on first boot, ~5 min, then cached) |

## Tested (regnet)

`tests/test_miner.py` drives the **real** miner via the `regtest_mine` command (not the regnet-only
generator): it asserts the miner **embeds a pending mempool tx** and advances the chain, that the
**coinbase carries the `hf2` signal**, and — via `regtest_powcheck`, run inside the node where the Heavy3
mmap lives — that the **dual-algo PoW switches** (sha224 vs blake2b give different difficulties).
