# doc/41 — hf2: free-form coinbase `operation`/`openfield` (miners carry no mandatory message)

**Status:** IMPLEMENTED + regnet-validated end-to-end (node consensus + solo miner + regnet + pool server).
Gated on `node.fork_height` (folds into the single hf2 fork — no new signal). Builds on doc/29
(binary/integer serialization) and doc/19 (coinbase VM state-root commitment).

> **Resolved — supersedes the shipped hf2 §2.C "coinbase slots must be empty" rule.** §2.C had merely
> emptied the coinbase `signature`+`public_key` slots to trim spam; it was not a load-bearing invariant
> (the coinbase is authorized by PoW + the reward formula). doc/41 REUSES those freed slots for the mining
> header (slot[4]=PoW nonce, slot[5]="vmsr"<root>[+signal] commitment), which is what frees
> `operation`/`openfield` to be optional free-form miner data. hf2 is not yet active on mainnet, so this
> pre-activation refinement landed as ONE unit: serialization core + `SignerFactory` rule (no longer
> requires empty; recipient==address kept) + digest readers (nonce from slot[4], state root from slot[5],
> coinbase never sig-verified/replay-checked) + miner.py/regnet.py/optipoolware.py construction + the
> fork-signal reader reading per-era (openfield pre-fork, public_key post-fork) + the §2.C tests inverted.
> Validated on live regnet: test_hf2_fork_transition, test_vm_post_fork, test_regnet_dual_pow,
> test_fork_wiring, test_pool_integration.
>
> **Follow-up (NOT in this change):** the GPU miner HOST drivers. `gpuminer/optihash.py` (CUDA, pool
> miner) submits a BARE nonce and the pool builds the coinbase — already doc/41-compatible; it only needs
> the background agent's blake2b-Heavy3 `new_pow` port. `gpuminer/opencl_alt/miner.py` (solo miner) builds
> its OWN coinbase, so it needs the doc/41 construction — but it cannot be GPU-tested in CI.

## Goal

Post-hf2 a miner is **not required to populate `operation` or `openfield`**, may put **arbitrary
bytes** there, and **nothing is enforced** on them (no length cap, no mandatory marker). For *every*
transaction (not just the coinbase) an empty `operation`/`openfield` is **omitted** from the
signed/hashed pre-image (compaction), so empty fields cost ~0 bytes and change nothing a wallet must
think about.

## The problem this solves

Today the coinbase `openfield` is the most overloaded field on the chain — a packed string carrying
THREE consensus payloads at once (see doc/19, fork.py, digest_tx.py):

```
openfield = [FORK2_SIGNAL "hf2"]  +  embed_state_root = "vmsr"<64-hex root>  +  <PoW nonce>
```

- **PoW nonce** — `miner.py:86,119` writes it; `digest_tx.py:73` reads `received_openfield[:128]` →
  `mining_heavy3.diffme_heavy3` (the PoW input).
- **VM pre-state root** — `vm_engine.embed_state_root`; `digest.py:706` extracts it and `digest.py:711`
  **hard-`raise`s post-fork if absent** (silent-divergence guard, doc/19).
- **hf2 fork signal** — `fork.has_fork_signal` / `db_fork_signal_reader`; also `rest_api.py:743`.

So a miner literally cannot leave `openfield` empty without breaking PoW verification, VM-divergence
detection, and fork signaling. "Nothing enforced" is impossible while these ride in `openfield`.

## Design — relocate the load-bearing payloads into the coinbase's freed slots

hf2 coinbase compaction (doc/29 §2.C) already makes the coinbase `signature` and `public_key` wire
fields **empty** — the coinbase is authorized by PoW + the reward formula, never by a signature. Those
two slots are dead weight. We **repurpose them post-fork as the coinbase mining header**, with NO
schema change (the wire stays the 8-field tuple, the DB columns are unchanged):

| 8-tuple slot | pre-fork (legacy) | **post-fork coinbase** |
|---|---|---|
| `[4] signature`  | RSA sig | **PoW `nonce`** (raw, ≤255 bytes, `lp1`) |
| `[5] public_key` | b64 pubkey | **mining commitment** = `"vmsr"<64-hex root>` + optional `"hf2"` vote |
| `[6] operation`  | op / "0" | **free-form, optional, uncapped miner data** |
| `[7] openfield`  | nonce+root+signal | **free-form, optional, uncapped miner data** |

Regular (non-coinbase) txs keep `signature`/`public_key` as the signature + key; only their
`operation`/`openfield` gain omit-when-empty (below).

### Pre-image / block-hash commitment (consensus)

The nonce and the state-root commitment MUST stay committed to the post-fork block hash (else a miner
could grind/forge them). So `_v2_tx_bytes(is_coinbase=True)` commits the freed slots as the mining
header instead of omitting them:

```
coinbase v2 pre-image =
  ts_cs(u64) | amount(u64) | addr(lp1) | recip(lp1)
  | nonce(lp1)              # from slot[4]
  | commitment(lp1)         # from slot[5]: "vmsr"<root>[+"hf2"]
  | FLAGS(u8)               # bit0 operation present, bit1 openfield present
  [ | operation(lp4) ]      # only if present (non-empty)
  [ | openfield(lp4) ]      # only if present (non-empty)
```

Non-coinbase v2 pre-image (`signature_buffer_v2` and `_v2_tx_bytes`) gains the same trailing
`FLAGS | [operation] | [openfield]` shape — empty fields are absent, not zero-length-prefixed.

`FLAGS` disambiguates "field absent" from "field present and empty" deterministically; two
implementations cannot diverge on quoting. The u8 `nonce`/`commitment` length prefixes match the
historical `[:128]` nonce bound and the fixed 4+64(+3)-byte commitment.

### Enforcement removed (post-fork only)

- `operation` 30-char cap and `openfield` 100000-byte cap are **not applied to the post-fork
  coinbase** at all, and remain only as anti-DoS *mempool* bounds for *non-coinbase* txs (the coinbase
  never transits the mempool). The miner may append any bytes.
- The config-driven `mandatory_message` rule (mempool.py) is unchanged (never applied to a coinbase).

### Readers move, fork-gated (pre-fork path byte-identical)

| reader | pre-fork source | post-fork source |
|---|---|---|
| `digest_tx` nonce | `openfield[:128]` | `signature` slot |
| `vm_engine.extract_state_root` | `openfield` | `public_key` slot |
| `fork.has_fork_signal` / `db_fork_signal_reader` | `openfield` | `public_key` slot |
| `rest_api` `/api/fork` query | `openfield LIKE` | `public_key LIKE` |

Each reader selects by `block_height >= node.fork_height` (or, for the coinbase row, by the
compaction marker = empty legacy sig on the reward row). Below the fork, every byte form is unchanged
and locked by `tests/test_characterization.py`.

### Writers (miner-facing)

`miner.py` / `pool/optipoolware.py`: post-fork, write `nonce` → signature slot and
`"vmsr"<root>[+"hf2"]` → public_key slot; leave `operation`/`openfield` empty unless the operator
chooses to stamp their own data. The miner no longer assembles a packed `openfield` string.

## Safety / invariants

- One fork only (hf2); no second signal. Pre-fork serialization, PoW, VM, and fork-signal reads are
  byte-for-byte unchanged (characterization-locked).
- Nonce + state-root commitment remain in the block-hash pre-image → PoW and divergence guards keep
  their teeth.
- v2 forms are still staged/inert (no mainnet activation yet), so changing the v2 pre-image now is not
  a live-consensus change; the characterization vectors for v2 are regenerated intentionally with this
  doc.
