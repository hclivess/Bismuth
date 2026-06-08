# 04 — Proof-of-Work (Heavy3) & difficulty

PoW lives in `mining_heavy3.py` (current), `mining.py` (legacy, unused), `hmac_drbg.py` (the DRBG
that seeds the lookup file), `difficulty.py` (retarget) and `difficulty_lwma.py` (the fork-gated LWMA
retarget — see [18](18-hardfork-hf2.md)). `miner.py` is the built-in solo miner that drives this PoW
([21](21-mining.md)). `digest.py` calls `check_block()` for every block; `difficulty()` is recomputed
after each block.

## Heavy3 algorithm

**The lookup file `heavy3a.bin`** — exactly **1 GiB (1,073,741,824 bytes)**. If absent it is
generated deterministically by `create_heavy3a()` using `hmac_drbg.DRBG` (HMAC-SHA512) seeded with a
fixed ASCII description of the element bismuth, written in 4 KiB chunks (~3 min). The seed must never
change. `mining_open()` mmaps it and validates two sentinel uint32s (`@0 == 3786993664`,
`@1024 == 1742706086`); a wrong size or sentinel triggers regeneration / `ValueError`. `RND_LEN` =
file size / 4 = 268,435,456 words. `mining_close()` unmaps on shutdown. When `heavy=False` (regnet),
the file is skipped and a no-op `anneal3_regnet` is used.

**Hashing (`diffme_heavy3`)**:
1. `h = sha224(pool_address + nonce + db_block_hash)` → 224-bit integer.
2. `anneal3(MMAP, h)`: the low 32-bit word selects a 32-byte-aligned offset into the 1 GiB file
   (`(word & ~0x7) % RND_LEN`); each of the seven 32-bit words of `h` is XOR-ed with consecutive
   file words and re-assembled into a 56-hex string. This forces a random read into a dataset far
   larger than CPU cache — the memory-hard property.
3. The achieved difficulty is the length of the longest prefix of `bin(db_block_hash)` that occurs
   as a substring of `bin(annealed)` (`bin_convert` expands each character to 8 bits).

**`check_block(block_height_new, miner_address, nonce, db_block_hash, diff0, received_timestamp,
q_received_timestamp, q_db_timestamp_last, peer_ip='N/A', app_log=None, new_pow=False)`** returns the
`diff0` to store and raises `ValueError` if the nonce fails. Acceptance:
- normal: `real_diff >= int(diff0)`;
- soft drop (block 180–360 s late): target reduced by `+1 - time_diff/180`, floored at 50;
- emergency drop (> 360 s late): target reduced by `-1 - 10*(time_diff-360)/180`, floored at 50.

In every case the **stored** difficulty is the original `diff0` ("lie about what diff was matched, so
the retarget algorithm isn't disturbed"). In regnet, `diff0` is forced to `REGNET_DIFF-8 = 8`.

`mining.py` is the pre-Heavy3 algorithm (single drop tier, no memory-hard step). It is **not imported
anywhere** and is retained only for reference.

### Dual-algo PoW (built, inert until signalled)

`diffme_heavy3`/`check_block` take a `new_pow` flag. Post the (separately-signalled) PoW fork the
**inner hash modernises** from `sha224(...)` to `blake2b(..., digest_size=28)`; the 1 GiB anneal and
the substring-difficulty measure are **unchanged**. The switch is purely `new_pow`, which the digester
sets to `block_height_new >= node.pow_fork_height` — derived deterministically from the on-chain `"pow2"`
fork signal (`fork.has_pow_signal` / `fork.dynamic_fork_height`). Miners advertise readiness by stamping
`pow2` into their coinbase (`pow_fork_signal=True`); until that fork locks in, every node mines and
validates today's sha224 Heavy3. See [21](21-mining.md) (solo miner / mining), [18](18-hardfork-hf2.md)
(the fork bundle), [19](19-vm.md) (VM).

## Difficulty retarget (`difficulty.py`)

`difficulty(node, db_handler)` returns
`(difficulty, diff_dropped, time_to_generate, diff_block_previous, block_time, hashrate,
diff_adjustment, block_height)` (10-dp floats, `block_height` int). On a brand-new chain it returns
`[24,…]`; on regnet it returns `REGNET_DIFF = 16` immediately.

Mechanism (target = **60 s/block**, window = **1440 blocks**):
1. Read the last two reward-bearing blocks and the 1440-block-old timestamps; derive the current and
   previous rolling average block times.
2. Implied `hashrate = 2^(prev_diff/2) / (block_time * ceil(28 - prev_diff/16))`.
3. `difficulty_new = (2/ln2) * ln(hashrate * 60 * ceil(28 - prev_diff/16))`.
4. PD term: `difficulty_new -= 10 * (block_time - block_time_prev)` (gain `Kd = 10`).
5. Damp: `diff_adjustment = (difficulty_new - prev_diff) / 720`, clamped to `≤ +1.0`.
6. `difficulty = quantize_ten(prev_diff + diff_adjustment)`.
7. Compute the broadcast `diff_dropped` (same 180/360 s drop tiers as `check_block`).
8. Floor both `difficulty` and `diff_dropped` at **50**.
9. At `height == POW_FORK - FORK_AHEAD`, call `fork.limit_version(node)`.

## Key constants

| Constant | Value |
|---|---|
| target block time | 60 s |
| difficulty window | 1440 blocks |
| PD gain `Kd` | 10 |
| damping divisor | 720 |
| min difficulty (floor) | 50 |
| soft/emergency drop thresholds | 180 s / 360 s |
| `heavy3a.bin` size | 1 GiB; `RND_LEN` 268,435,456 words |
| sentinels | word@0 = 3786993664, word@1024 = 1742706086 |
| `REGNET_DIFF` | 16 (effective `check_block` diff 8) |

> Note (documented in [14](14-known-issues-and-improvements.md)): `difficulty.py` clamps the
> per-block adjustment only on the upside; and the `ceil(28 - diff/16)` term goes non-positive at
> `diff ≥ 448` (far above current values). `bin_convert` operates on the *characters* of the hex
> string, not the raw bytes — intentional and consensus-fixed.
