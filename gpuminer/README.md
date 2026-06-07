# Bismuth GPU miner

GPU miners for Bismuth's **Heavy3** proof-of-work, vendored into the main tree so the miner and the
node stay in lockstep (this matters across the hard fork — see the caveat below).

Two implementations are included:

| Dir | API | Source | Notes |
|---|---|---|---|
| `./` (`bis.cu`, `optihash.py`) | **CUDA** (NVIDIA) | [`bismuthfoundation/kbkminer`](https://github.com/bismuthfoundation/kbkminer) | current; mines today's Heavy3; pool-based |
| `opencl_alt/` (`bismuth.cl`, `clminer.py`) | **OpenCL** (AMD/portable) | [`bismuthfoundation/Bismuth-GPU-miner`](https://github.com/bismuthfoundation/Bismuth-GPU-miner) | older; kept for AMD/cross-vendor |

Copyright/attribution and licensing are preserved from the upstream repos (`LICENSE`,
`README_kbkminer.md`). Original authors: Hclivess, Primedigger, Maccaspacca, SylvainDeaure (2017),
kbk and geho2 (2021); OpenCL miner by gladimor.

## How it works

Heavy3 = `sha224(miner_address + nonce + block_hash)` → **anneal** (7 random-access XOR lookups into
the 1 GB `heavy3a.bin` "junction noise" file — the memory-hard step) → the difficulty is the longest
prefix of the block hash's bits that appears as a *substring* of the annealed result. The GPU loads
that 1 GB table into VRAM (hence the ~1.1 GB minimum) and brute-forces nonces in parallel; candidate
nonces are verified on the CPU (`mining_heavy3.diffme_heavy3`) and submitted.

Both miners are **pool miners**: they `getwork` from an Optipoolware pool and submit solved nonces
back. (Solo mining would point them at a local pool.)

## Build & run (CUDA / kbkminer)

```
sudo apt install nvidia-cuda-toolkit cmake python3-pybind11
cd gpuminer && chmod +x mycompile.sh && ./mycompile.sh     # builds bis.so
cp bis.so optihash.py miner.txt ..                          # next to node.py
# edit miner.txt: miner_address, mining_ip (the pool), port
python3 optihash.py
```

Run a synced node + an Optipoolware pool first; start the miner only once the node is at the tip.

## ⚠️ Two caveats, stated plainly

1. **Not GPU-tested in this repository's CI.** The build host here has no GPU, so these are vendored and
   reviewed at the source level but not re-run end-to-end. Validate on real NVIDIA/AMD hardware before
   relying on them. Upstream kbkminer is known-good on NVIDIA + CUDA 10.1 / driver 470.
2. **Coupled to the PoW.** The kernels implement *today's* Heavy3 exactly. If the planned hard fork
   changes or improves Heavy3 (see [`../doc/18-hardfork-hf2.md`](../doc/18-hardfork-hf2.md)), the
   kernel (`bis.cu` / `bismuth.cl`) **must** be updated to the new algorithm and re-validated, or the
   miner will produce invalid post-fork blocks. The miner and `mining_heavy3.py` must always agree.
