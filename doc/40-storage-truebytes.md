# doc/40 - hf2 Stage-4: TRUE-BYTES LMDB storage for every storage type

Status: **design complete + foundational codecs implemented & tested**; the consensus-path wiring is staged behind the validation gates in the Validation section. This is Stage 4 of the one hf2 serialization rework (doc/29): retire the base64/hex/text at-rest forms across the LMDB store for true bytes, gated on the single `node.fork_height` by destination height. **Pre-fork (`fork_height is None`, i.e. mainnet today) every path is byte-identical to the current store - inert on prod until on-chain lock-in.**

> How this spec was produced: 10 storage domains + 1 gap domain were each designed, adversarially red-teamed for consensus / reorg / byte-identity / A-hex safety, then corrected and re-verified in a second round against a pinned shared-conventions contract. The review caught real consensus-forking defects (dropping the coinbase sig/pubkey while the frozen `_v2_tx_bytes` block-hash pre-image still commits to them; folding the VM storage key would reorder `state_root()`; RSA is *single* base64, not double) - all fixed below. Residual per-domain items are listed as implementation checklists.

## Implementation status (this commit)

| Piece | Status | Where |
|---|---|---|
| Signature codec (per-scheme true bytes, RSA single-b64, 0x40 recoverable hex, 0x00 opaque fallback) | **implemented + tested** | `sigbytes.py`, `tests/test_storage_codecs.py` |
| Address/recipient codec (tagged union + verbatim 0xFF fallback, round-trip guarded) | **implemented + tested** | `addrbytes.py`, `tests/test_storage_codecs.py` |
| `block_store` write/read wiring of the codecs (fork-gated by destination height) | **wired + tested**; production flip still behind the §Validation replay gates | `block_store.py` `put_blocks`/`_expand`, `storage_backend.py` `LmdbWriteBackend`, `node.py` |
| tx-fields codec (timestamp varint + amount/fee/reward integer units, storage-mode aware) | **implemented + wired + tested** | `txfields.py`, `tests/test_storage_codecs.py`, `block_store.py` |
| difficulty store (closes the misc-table gap: difficulty_e10/solvetime/cumulative_work, deterministic work) | **implemented + tested** (codec); env+wiring staged | `diff_work.py`, `diff_store.py`, `tests/test_storage_codecs.py` |
| core-indexes (txid_index raw-32 key, block_store `hashes` raw-digest key, balance_index u128 LE) | **implemented + tested** | `txid_index.py`, `block_store.py`, `balance_index.py`, tests re-baselined |
| vm | storage **already true-bytes** (raw `addr:word` keys, raw 32-byte balances); surface-A (openfield root) rides with coinbase; surface-B (key fold) **rejected** (state_root reorder) |
| coinbase | **blocked** — can't drop the coinbase sig/pubkey until doc/29 §2.C changes the *wire* pre-image (else forks the block hash) |
| plugin-stores: shielded raw-byte note_id / key-image (nullifier) keys | **implemented + tested + multinode-validated** | `shieldedv1.py` `_kb`, `tests/test_shielded_kvstore.py` |
| block-header txids forward list / mempool LMDB record / plugin token-amount varint | designed + verified (this doc); staged | per-domain sections |

**Shielded nullifier raw keys (consensus-sensitive — landed with its own multinode pass).** The shielded
sidecar now stores note_id (32-byte blake2b) and key-image (33-byte compressed point — the spent-set /
nullifier) as RAW bytes, not 64/66-hex `.encode()`. Set membership is preserved because add/check/rollback
all funnel through one `_kb` helper, and the key image is already canonicalized to compressed form by
`_verify_ring` (shieldedv1.py:625) BEFORE the spent-set check — so no compressed-vs-uncompressed
double-spend bypass is introduced. Shielded is post-fork-only + rebuildable (no legacy data to migrate).
Validated by the full shielded suite (spends/key-images/rollback) + the 3-node integration test
(`shieldedv1` stats + note identical across A,B,C).

### Multinode validation (the storage changes proven across nodes)

The 3-node regnet integration test (`tests/test_multinode_integration.py`, `BISMUTH_RUN_MULTINODE=1`),
with `block_store` + `balance_index`=primary + `txid_index` enabled and mining **across the fork**, passed
with all storage agreements green:
- `CHAIN PARITY ok: blocks 1..27 byte-identical across A,B,C`
- `block_store ok: bodies at heights [1,2,13,26,27] identical across A,B,C` — the `_expand` reconstruction
  of the packed sig/address/tx-fields + raw `hashes` keys agrees byte-for-byte across 3 independently-built
  stores, **including post-fork heights** (where the packing actually runs).
- `balance_index ok` (u128 LE) · `txid_index ok: 3 post-fork txid height(s) identical` (raw-32 keys) ·
  `vm_state` · `token_index` · `shieldedv1` all agree · difficulty detector clean (median 16.0).
The 2-node signature harness (`tests/test_two_node_signatures.py`, `BISMUTH_RUN_TWONODE=1`) also passed —
every signing scheme verifies and syncs across 2 nodes. So the post-fork true-bytes storage is
consensus-equivalent across nodes, not just in single-process unit tests.

### Probe finding (drove the tx-fields simplification)

Direct probes of the consensus path proved that **the `0` vs `0.00000000` question and the signature
handling are already resolved at the hf2 consensus level**, so the storage layer must preserve
*consensus-equivalence*, not the legacy SQLite byte-form:
- `block_hash_v2` and `tx_id_v2_s` funnel every amount through `_v2_units` (integer atomic units), so `'0'`
  and `'0.00000000'` produce the **identical** block hash and txid (the legacy sha224 was form-sensitive —
  that is the historical "mess", now frozen). 
- Post-fork the signature signs the **content txid** via ecrecover (single-sig pubkey dropped); dedup is
  keyed on the content txid, not `signature[:56]` (that is pre-fork only).

Therefore `txfields.py` stores **integer units** (varint) and reconstructs the canonical string on read —
`'0'` reconstructs as `'0.00000000'` (decimal mode), a deliberate, consensus-safe normalization. No
per-field `BARE/FIXED8/RAWTEXT` render-mode tag (the byte-for-byte-SQLite design above) is needed; that
complexity was solving a legacy-SQLite-parity problem hf2 already dissolves. The correctness gate for the
money fields is **consensus-equivalence** (`block_hash_at`/`tx_id_at` recompute, which is form-invariant),
not byte-identity against the retiring SQLite row. Timestamp reconstructs byte-identically (`'%.2f'`). The
codec is **storage-mode aware** (`amounts.LEDGER_INTEGER`): decimal → `to_units`/`from_units`; integer →
`int`/`str(units)` (the residual fix — in integer mode the stored string already IS units).

The two codecs are now wired into `block_store`: `put_blocks(items, fork_height=None)` packs the
signature/address/recipient fields to true bytes for blocks at `height >= fork_height` (msgpack codec
required), and `_expand` rebuilds the exact wire strings on read, dispatching by value type (`bytes` =
packed, `str` = legacy passthrough). `LmdbWriteBackend` reads the node's **live** `fork_height` at append
time, so the gate is by destination height with no second signal. **`fork_height=None` (mainnet today,
and the default used by `build_from_sqlite`/`verify_against_sqlite`) keeps every field as the legacy
`str` — byte-identical to the current store, inert on prod.**

Tested: 26 codec round-trip tests + new `block_store` post-fork tests (realistic values pack to raw bytes
and reconstruct byte-for-byte; a pre-fork block stays legacy `str`; synthetic non-canonical values
round-trip via the verbatim/opaque fallback; straddling chain). The wider storage/serialization/fork
suite (block_store, storage_backend cross_check, characterization, replay, kvstore, balance/txid index,
integer storage, signature types, reward chain, LWMA, fork transition, consensus invariants, regnet
dual-PoW mine, digest/mempool/REST) is green. The deeper production go-live gate — a full straddling
regnet chain mined **with `block_store` enabled** plus `replay_verify` 0-mismatch + `cross_check`
byte-for-byte — remains as specified in §Validation before any consensus-read flip.

## Miner & pool impact: NONE (verified)

The bundled miner (`pool/optihash/optihash.py`) and pool (`pool/optipoolware.py`) need **zero** changes for this storage rework - it is node-internal and every form they consume is reconstructed byte-identically at the boundary:

- The pool reads `/api/fork`, `/api/vm/contracts.state_root`, `/api/status.last_block_hash`, `/api/difficulty`, `/api/mempool`, `/api/address/<a>/transactions` - all canonical wire/hex forms (C-RECON keeps these stable at the REST boundary).
- The pool builds the same 8-field wire coinbase and RSA-signs the reward over the legacy buffer (doc/29 Stage 2); the node compacts it on *storage*, transparently.
- The miner treats `blockhash` as an opaque string in the Heavy3 PoW input, so the 56->64-hex blake2b block-hash change just flows through (already covered by the dual-algo PoW work).

**Cosmetic fix applied this commit:** the pool payout log used `signature_enc[:56]` (the *legacy* txid slice); post-fork the canonical txid is `blake2b(content)`, so the pool now prefers the node's returned `txids[0]` from `POST /api/transaction`. Log-only, no consensus impact.

**Forward item:** once doc/29 §2.C drops the dead RSA sig+pubkey from the coinbase *wire* form, the pool could stop RSA-signing the coinbase reward. Until then it keeps signing it (see Coinbase).

---

## SHARED CROSS-DOMAIN CONVENTIONS

> This section is the **spine** of the Stage-4 true-bytes LMDB spec. Every storage
> domain (block-header, tx-fields, signatures, pubkeys, core-indexes, plugin-stores,
> shielded, vm, reward_chain, mempool) MUST obey it verbatim. Where a domain spec
> disagrees with this section, **this section wins**. Each convention below is a
> shared encoding that two or more domains touch; pinning it here is what stops the
> domains from silently disagreeing on bytes.

### C0. Governing invariants (apply to every convention below)

- **One fork signal.** Every *consensus* gate reads `node.fork_height` and nothing
  else (set/persisted at `digest.py:542-557`; dispatched at `bismuth_serialize.py:151-175`,
  `digest.py:624-626`, `digest_tx.py:114`). There is no second fork flag.
  `amounts.LEDGER_INTEGER` (`amounts.py:65`) is a **storage** flag and is decoupled
  from `fork_height`: it changes how a row is stored/reconstructed, never *whether*
  a height is consensus-post-fork.
- **Gate by destination height, never a global mode.** A block/tx is "v2" iff its
  own `block_height >= fork_height`. `fork_height is None` ⇒ 100% legacy path (this
  is mainnet today). No process-wide "we are now in v2 mode" switch exists.
- **Pre-fork byte-identity is frozen.** The consensus pre-image functions
  `signature_buffer` / `tx_id` / `block_hash` (`bismuth_serialize.py:23-55`) and
  their v2 siblings `signature_buffer_v2` / `tx_id_v2` / `block_hash_v2` /
  `_v2_tx_bytes` (`bismuth_serialize.py:77-147`) are NOT mutated by any storage
  change. Storage stores compact; **reconstruction (C-RECON) rebuilds the exact
  frozen wire strings on read.**
- **A-hex regression ban.** "Raw" means **true bytes in an LMDB key or value**,
  never hex-in-text. Hex doubles size (2×); base64 is 1.33×; raw is 1×. Any field
  this spec calls "raw" that is found stored as hex/base64 text is an automatic
  reject.
- **Legacy on-disk byte-identity.** Existing mainnet LMDB files stay readable
  as-is. New binary forms apply **only to the v2 region** (`height >= fork_height`);
  the legacy region keeps its current on-disk encoding. The 23 GB prod ledger is
  **never force-rebuilt**, and is **never hot full-scanned**; projection rebuilds
  run from a snapshot copy (see C7).
- **Replay-validated.** Before any `*_consensus` read flips to primary:
  `replay_verify` reports **0 mismatches** at `fork_height=None` AND across a
  straddling (pre+post-fork) regnet chain; `storage_backend.cross_check`
  (`storage_backend.py:130`) and `block_store.verify_against_sqlite` stay
  byte-for-byte on the **reconstructed** forms.

---

### C1. Height key — `Codec.hkey`

**Definition.** `Codec.hkey(height) = struct.pack(">Q", int(height))` — an
**8-byte big-endian unsigned uint64** (`kvstore.py:74-75`; inverse
`Codec.unhkey` `kvstore.py:78-79`).

**Used by (must agree byte-for-byte):** `block_store.blocks` keys and
`block_store.hashes` *values* (`block_store.py`); `reward_chain.rewards` keys;
`token_index` composite-key height tails (`cred`/`deb`/`journal`/`alias_rev`/
`ajournal`, `struct.pack(">QQ", height, seq)`); `shieldedv1` composite-key height
prefixes (`notes_h`/`kimg_h`/`flows`); `txid_index` *values* (`h.to_bytes(8,"big")`).

**Rationale.** Big-endian ⇒ byte-lexicographic order equals numeric order, so LMDB
range scans (rollback, LWMA windows, height-bounded sums) are correct on both
LMDB and SQLite-KV backends without a comparator.

**HARD INVARIANT — keyspaces cannot alias.** `>Q` is **positive-only**; it cannot
encode a negative height. Therefore:
- `block_store` is **positive-real-height-only**. It has no row for the legacy
  negative-height reward-mirror entries.
- `reward_chain` **owns** the negative-height rows by **negating** them to a
  positive key (`reward_chain.py` `extract_from_ledger` negates `block_height < 0`
  to a positive `hkey`).
- These two keyspaces **MUST live in SEPARATE LMDB envs** (see C7) so a real
  positive block height can never numerically collide with a negated reward
  height. No env may contain both a `block_store.blocks`-class key and a
  `reward_chain.rewards`-class key. The invariant "block_store heights are real
  positive; reward_chain heights are negated-from-negative; they never share an
  env" is normative, not advisory.

---

### C2. `blake2b` digest_size — pinned per use

A bare `blake2b` is **forbidden** in any domain spec; every use MUST cite its
`digest_size`.

| Use | `digest_size` | Output at boundary | Site |
|---|---|---|---|
| Block hash (`block_hash_v2`) | **32** | 64-hex | `bismuth_serialize.py:130-147` |
| TX id (`tx_id_v2`) | **32** | 64-hex | `bismuth_serialize.py:93-98` |
| Signature pre-image hash inside the above | **32** | — | `bismuth_serialize.py:77-98` |
| `block_store` pubkey-dedup key (`pk`) | **32** | raw 32B key | `block_store.py:76-79` |
| Per-env projection state-root (C7) | **32** | raw 32B / 64-hex | `vm_state.py:97` (template) |
| **PoW Heavy3 inner** | **28** | — | doc/18 §D |

**Rationale.** Two widths are genuinely in play: **32** for all consensus
hashes / txids / block hashes / storage dedup / state roots, and **28** for the
PoW Heavy3 inner digest only. A domain that writes "blake2b" without a width risks
defaulting to the wrong one (the PoW domain in particular). Pin it everywhere.

---

### C3. Canonical txid byte form

**Post-fork there is exactly ONE txid byte form.** The canonical content-txid is
`tx_id_v2` = blake2b-256 (digest_size=32) of the v2 signature pre-image
(`bismuth_serialize.py:93-98`), dispatched by destination height via `tx_id_at`
(`bismuth_serialize.py:166-175`, gate `digest.py:105-107`).

- **In keys and values:** store the **raw 32 bytes**.
- **64-hex lowercase:** permitted **only at the API/REST boundary** (and in the
  legacy on-disk region, untouched).
- **Every store that keys or dedups by txid MUST use the raw-32 form:**
  `txid_index.txid` keys, `token_index.seen` keys, and any future dedup set. Today
  `token_index.seen` and `shieldedv1` note-id/key-image store **hex text**
  (`token_index.py`, `shieldedv1.py:144-145,294-295`); for the v2 region these
  become raw bytes (this is a deliberate, fork-gated format break — re-baseline its
  characterization test per C6/C7, not a silent flip).

**Legacy dedup boundary (call-out).** Pre-fork, duplicate detection uses
`signature[:56]` (the legacy signature-slice dedup), **not** a content txid. That
boundary stays exactly as-is in the legacy region. The raw-32 content-txid is the
dedup identity **only for `height >= fork_height`**; `txid_index` itself is
populated for post-fork txs only. A cross-check that compares "txid dedup" across
the boundary MUST compare legacy `signature[:56]` below the fork and raw-32
content-txid at/above it — never assume one form spans both.

---

### C4. Amount units

**All amounts are integer atomic units: `1 BIS = 100_000_000` units**
(`amounts.SATOSHIS_PER_BIS`, conversions `amounts.to_units` / `from_units`
`amounts.py:23-36`). This is identical across **every** domain that holds an
amount: `block_store` stored-tx `amount`/`fee`/`reward`; `balance_index`
`[credit_units, debit_units]`; `reward_chain` entry `amount_units`; `token_index`
`cred`/`deb` amounts; `shieldedv1` `flows`/`notes` amounts; `vm_state`
`balances`/`storage` (uint256 units).

**Reconstruction discipline (C-RECON, normative).** Any domain that round-trips a
stored amount back into a **consensus pre-image** (signing buffer, REST/socket
sync wire form, re-digest) MUST pass it through `amounts.consensus_amount`
(= `from_units` in integer mode) `amounts.py:65-72`. **Never** use
`display_amount` — it is a float and loses precision above 2⁵³ units, which forks
socket vs REST sync. This is the single discipline that keeps `balance_index` in
lockstep with `ledger_balance3` and keeps the two transports byte-identical.

**Bounded vs unbounded width.**
- **BIS amounts are bounded** (capped supply) ⇒ **u64 is sufficient** for compact
  storage of BIS amount/fee/reward/balance fields.
- **Token amounts are UNBOUNDED** (arbitrary issuer-defined supply) ⇒ they MUST
  use **varint or u128**, **never u64**. A u64 token field is an overflow bug, not
  a size optimization. (`vm_state` already uses 32-byte uint256 words, which is
  safe for both.) Any new compact token-amount encoding picks varint/u128.

---

### C5. Address byte form policy

**Address raw-byte migration is ALL-OR-NOTHING across every store that keys by
address.** Stores keyed by address: `balance_index.bal`, `token_index`
(`addrtok`/`cred`/`deb`/`alias_*` embeds), `vm_state` (`code`/`balances`,
`addr.encode()`), `shieldedv1`, plus address fields inside `block_store` rows.
Either all of these key by the same raw-byte address form, or none do. A mixed
state (e.g. `balance_index` raw bytes while `token_index` keeps `address.encode()`
base58 text) gives one account two on-disk identities and breaks every cross-store
lookup and parity assert.

**Consensus/dispatch path keeps the base58 STRING.** `SignerFactory` routes by
**regex on the base58 string form** (`signerfactory.py:120-134`: `^Bis1…$`,
`^Bism|mBis…$`, `^[abcdef0-9]{56}$`, versioned prefixes). Storage may move address
**keys** to raw bytes, but the recovered/dispatched address handed to
`signerfactory` MUST be reconstructed losslessly to its exact base58 string before
dispatch (C-RECON). Storage-raw, dispatch-string: the regex path is never fed raw
bytes.

---

### C6. Codec split — frozen-JSON vs msgpack

A blanket **"use the Codec everywhere" rule is FORBIDDEN** — it breaks the
byte-identity tests (`test_lmdb_on_disk_bytes_identical`, doc/36:269) for the
JSON-valued stores. The split is load-bearing and is pinned here:

| Codec | Stores | Why |
|---|---|---|
| **Frozen JSON** — `json.dumps(..., separators=(",", ":"))` | `token_index`: `tokreg`, `journal`, `alias_fwd`, `ajournal` (and the JSON-valued cred/deb metadata); `shieldedv1`: `notes` | Byte-parity with the legacy store; separators are exact and characterization-locked. Never routed through `Codec`. |
| **msgpack** — `Codec.pack` (`use_bin_type=True`, JSON fallback) | `block_store` values (`{"h":…, "t":[…]}`); `balance_index` (`[credit, debit]`); `reward_chain` (`[[sender, recipient, amount_units, mirror_hash], …]`) | Compact binary; no legacy text-parity constraint. |
| **Raw bytes — no codec** | `block_store.pkr` (raw pubkey), `block_store.hashes`/`txid_index` values (raw `>Q`/8B BE), all of `vm_state` (32B BE words), key-only sentinel dbs (`notes_h`/`kimg_h`, `seen`, `alias_rev`) | True bytes; A-hex ban applies. |

**Any JSON→binary (or any codec) flip is a deliberate, fork-gated format break**
applied to the v2 region only, with its **own characterization re-baseline**. It
is never a silent change and never a blanket sweep. (This is the only sanctioned
path to, e.g., turning `token_index` string-int counters into BE-uint64/varint, or
`token_index.seen` hex into raw-32.)

---

### C7. Per-env layout, state-roots, snapshots, and the migration flip

**Per-env layout.**
- The **canonical `block_store` lives in its own append-only env** with generous
  `map_size` headroom (size the 23 GB prod ledger env for 100 GB+). LMDB's
  copy-on-write B+tree free-list reclaims pages; compaction is **offline
  `mdb_copy --compact`**, never an in-place hot operation on prod.
- **Every rebuildable projection lives in its OWN separate env**: `balance_index`,
  `txid_index`, `token_index`, `shieldedv1`, `reward_chain`, `vm_state`. Separate
  envs mean a projection `drop+rebuild` never fragments the canonical store, and
  (per C1) `reward_chain`'s negated keyspace can never alias `block_store`.
- **No hot full-scan of `block_store` on prod** to rebuild a projection (per the
  no-heavy-scans memory) — rebuild from a snapshot copy.

**Per-env state-root.** Each projection env carries a **sorted-kv blake2b-256
(digest_size=32) state-root** (template: `vm_state.py:97-119`, blake2b-32 over
sorted `code+storage+balances`). `balance_index`, `token_index`, `shieldedv1`,
`reward_chain` each get an analogous root so an env is self-verifying against the
tip block.

**Snapshot format.** A snapshot is **the compacted LMDB env files themselves**
(`mdb_copy --compact`, preserving the proven byte-identical on-disk format —
doc/36:269), **plus a manifest**:
`{tip_height, tip_hash, fork_height, per-env state-root}`. Snapshots are NEVER a
re-serialized dump (that would void byte-identity); restore is O(copy).

**Migration flip (per-projection, gated).** The SQLite→LMDB **consensus-read** flip
is gated **per projection**, never all-at-once. For each projection: run in
**shadow + `parity_strict`** through `storage_backend.cross_check`
(`storage_backend.py:130`) extended to assert that projection's invariant
(`balance_index` vs `ledger_balance3` doc/26:186; `txid_index` vs signature-scan
dedup; `reward_chain` vs negative-height-row sum; `token_index` cred/deb vs SQLite
`SUM`/`GROUP BY`; `shielded` pool vs `ledger_balance(SHIELD_SINK)`). Only after
`replay_verify` reports **0 mismatches over a straddling chain** may that one
projection's `*_consensus` flag flip to primary.

---

### C8. Format-version sentinel

**Every store env carries a 1-byte format-version in its `meta` db** (e.g.
`meta[b"fmt"] = b"\x04"` for Stage-4 true-bytes). On first open under new code:
- If the sentinel is **absent or lower** than the code expects (e.g. an
  operator-copied old hex-keyed env), the store **force drop+rebuilds** (projections)
  or **refuses to serve consensus reads until rebuilt** (canonical), rather than
  silently returning misses for keys that exist under the old encoding.
- The sentinel is written atomically with the rebuild's first commit.

**Rationale.** Without this sentinel, an env copied from a pre-Stage-4 node (hex
keys, base64 values) would open cleanly and **silently miss every lookup** because
the new code probes raw-byte keys that don't exist in the old layout. The 1-byte
version makes the encoding mismatch loud and self-healing instead of a silent
correctness hole.
```

That is the complete, self-contained `SHARED CROSS-DOMAIN CONVENTIONS` section, ready to paste verbatim into the Stage-4 spec. Source files cited inline (`bismuth_serialize.py`, `amounts.py`, `kvstore.py`, `block_store.py`, `token_index.py`, `txid_index.py`, `balance_index.py`, `reward_chain.py`, `vm_state.py`, `shieldedv1.py`, `signerfactory.py`, `storage_backend.py`, `digest.py`, `digest_tx.py`) all live under `/root/bismuth-claude/Bismuth/`.

---

# Per-domain true-bytes designs


---

## 1. Transaction signature field (per signer scheme)  `[signatures]`

## hf2 Stage-4 TRUE-BYTES LMDB: Transaction Signature Field (per signer scheme)

> Obeys the **SHARED CROSS-DOMAIN CONVENTIONS** verbatim. In particular: C0 (one fork
> signal `node.fork_height`; gate by destination height; pre-fork byte-identity frozen;
> A-hex ban; replay-validated; legacy on-disk byte-identity; no hot full-scan), C2 (every
> `blake2b` cites its `digest_size`), C6 (Codec split — `block_store` values are msgpack),
> C7 (`block_store` is the canonical append-only env; cross-check seam), and C8 (1-byte
> `fmt` sentinel). Where this section and the shared section disagree, **the shared
> section wins.**

### 0. Scope and where the bytes live

The transaction `signature` is field index 4 of the canonical 8-field tx
(`bismuth_serialize.py:19-20`) and field index 4 of the 11-field stored row (after
`block_height` is dropped as the LMDB key; the stored-row layout is
`timestamp,address,recipient,amount,signature,public_key,block_hash,fee,reward,operation,openfield`,
`block_store.py:48-50`). In the current LMDB store it is carried as a **base64 text string**
inside the per-block msgpack value (C6 — `block_store` is a msgpack store, not JSON, not raw):

```
block_store.blocks[ Codec.hkey(height) ]                       # 8B BE uint64 key (C1)
  = Codec.pack({"h": block_hash_str,                           # msgpack value (C6)
                "t": [ [ts, addr, recip, amount, signature,
                         pk_id, block_hash, fee, reward,
                         operation, openfield], ... ]})
```

(`block_store.py:55-57`, `:90-99`, `:102-115`.) The signature is stored today exactly as the
SQLite `signature` TEXT column held it — **single base64 text** for every legacy scheme — which
is the **A-hex-class regression in disguise** (C0): a binary artifact (the raw signature)
re-expanded ~1.33× by base64 and embedded inside a *binary* msgpack value where the inflation
buys nothing.

This section defines a per-scheme **TRUE-BYTES** encoding of that field: post-fork, the msgpack
tx-list carries the *decoded raw signature bytes* with a 1-byte scheme tag and a length
discipline; the exact legacy base64/hex wire string is reconstructed on read (C-RECON). It does
**NOT** touch any frozen consensus pre-image: `signature` is excluded from `signature_buffer` /
`tx_id` / `block_hash` and from `signature_buffer_v2` / `tx_id_v2` (`bismuth_serialize.py:23-29`,
`:77-98`) by construction; the only place the signature enters a pre-image at all is
`_v2_tx_bytes` for the *block hash* (`bismuth_serialize.py:112-127`), and there it is consumed
**AS STORED** with a length prefix — so the byte-identity rule (§3) keeps that pre-image stable.

### 1. The signature codec (`sigbytes.py`, new module)

A new module `sigbytes.py` provides:

- `pack_from_wire(signature_str, address) -> bytes` — WRITE: derive tag from `address`, strip the
  wire envelope to true bytes, emit the packed blob.
- `to_wire(blob) -> str` — READ: unpack, rebuild the byte-identical wire string.
- low-level `pack_signature(tag, raw) -> bytes` / `unpack_signature(blob) -> (tag, raw)`.

`pack_from_wire` is called at the post-fork write branch (§4); `to_wire` at the post-fork read
branch (§4).

#### 1.1 Packed signature blob (the value placed in tx field [4], post-fork)

```
field      | type        | width   | notes
-----------+-------------+---------+----------------------------------------------------------
tag        | u8          | 1       | scheme tag (§1.2); selects decode + rewrap rule
sig_len    | u16 LE      | 2       | length of raw signature bytes (0..65535)
sig_bytes  | raw bytes   | sig_len | the DECODED signature — TRUE bytes, NO base64, NO hex (C0)
```

Total stored = `3 + len(raw)`. **`sig_len` is dynamic and load-bearing: raw signature length
VARIES within a single scheme.** RSA in particular is **128 bytes for legacy 1024-bit keys**
(early mainnet, blocks h≈2..) and **512 bytes for 4096-bit keys** (later mainnet); secp256k1/r1
DER is 64–72 bytes; multisig blobs are variable (`k×(idx,len,DER)`). There is **no fixed-512 RSA
assumption** anywhere in this spec — the u16 length prefix carries the real width per row.

`u16` covers every scheme: the largest raw signature is ML-DSA-87 at 4627 bytes
(`signer_mldsa.py`, doc/20), far under 65535. Multisig blobs (`1 + k×(2 + ~71)`) are bounded by
the consensus `SIG_FIELD_MAX`-char field cap (`signer_multisig.py`), well under u16. (Forward-compat
ceiling: a future >64 KB signature scheme would need a width bump — see Risks.)

The `tag` makes the blob self-describing for `to_wire` without re-deriving the scheme from the
address on every read. The address still **authoritatively** selects the verifier on the consensus
path; the tag is a corruption cross-check (§4).

#### 1.2 Scheme tag values

```
tag | scheme                      | address dispatch (address_to_signer →)       | wire envelope (to_wire rebuild)
----+-----------------------------+----------------------------------------------+------------------------------
0x00| OPAQUE LEGACY TEXT (fallback)| (n/a — see §5)                              | sig_bytes verbatim as UTF-8
0x01| RSA                         | SignerRSA            (56-hex addr)            | b64encode(raw)         SINGLE
0x02| secp256k1 ECDSA (legacy DER)| SignerECDSA          (Bis1…, len<=50)         | b64encode(raw)
0x03| ED25519                     | SignerED25519        (Bis1…, len>50)          | b64encode(raw)
0x04| secp256r1 (P-256)           | SignerSECP256R1      (versioned prefix)       | b64encode(raw)
0x05| ML-DSA-44                   | SignerMLDSA44        (versioned prefix)       | b64encode(raw)
0x06| ML-DSA-65                   | SignerMLDSA65        (versioned prefix)       | b64encode(raw)
0x07| ML-DSA-87                   | SignerMLDSA87        (versioned prefix)       | b64encode(raw)
0x08| native multisig (M-of-N)    | SignerMultisig       (Bism…/mBis…)            | b64encode(raw blob)
0x09| BTC (test/interop)          | SignerBTC                                     | b64encode(raw)
0x0A| CRW (test/interop)          | SignerCRW                                     | b64encode(raw)
0x40| secp256k1 recoverable (hf2) | SignerECDSA single-sig, ecrecover            | raw.hex()  (130 lc hex)
```

The tag is assigned on WRITE from `SignerFactory.address_to_signer(address)`
(`signerfactory.py:120-134`) plus the post-fork single-sig discriminator
`SignerFactory.is_single_sig_ecdsa(address)` (`signerfactory.py:204-208`, which routes the
Ethereum-shape recoverable path, used by `verify_tx_signature` at `signerfactory.py:228-236`).
The value `0x40` flags the **one scheme whose wire form is lowercase hex, not base64** (the
recoverable sig, `signer_ecdsa.py:146-148`); `to_wire` branches on the **tag alone**, never
re-guessing from string shape.

### 2. Per-scheme decode (write) and rewrap (read) rules

For every scheme, WRITE strips the wire envelope to TRUE bytes; READ rebuilds the byte-identical
wire string (C-RECON, round-trip proof in §3). Verification needs **no rewrap**: every signer
exposes `verify_bis_signature_raw(raw_sig, raw_pubkey, buffer, address)` that consumes the raw
form directly (`signer_rsa.py:142-160`, `signer_ecdsa.py:119-129`, `signer_ed25519.py:144-148`,
`signer_secp256r1.py`, `signer_mldsa.py`, `signer_multisig.py:194-196`), dispatched via
`SignerFactory.verify_bis_signature_raw` (`signerfactory.py:197-201`).

- **RSA (tag 0x01) — SINGLE base64.** Wire is ONE base64 of the raw PKCS#1 v1.5 signature.
  Authoritative source: `verify_bis_signature` does exactly one `b64decode(signature)`
  (`signer_rsa.py:124`) and `sign_buffer_for_bis` does exactly one `b64encode`
  (`signer_rsa.py:168-171`); `verify_bis_signature_raw` takes the singly-decoded raw
  (`signer_rsa.py:142-156`). Therefore:
  - **Write:** `raw = b64decode(wire)`.
  - **Read:** `wire = b64encode(raw).decode()`.
  - **Raw width is VARIABLE: 128 B (1024-bit legacy keys) or 512 B (4096-bit keys).** Both eras
    exist on mainnet (verified by read-only single-row sampling: h=2..4 → 172 sig chars → 128 B
    raw; h≈500000..1900002 → 684 sig chars → 512 B raw; a *second* b64decode either raises
    `binascii.Error` or yields non-deterministic garbage, proving the wire is **single, not
    double, base64**). The u16 `sig_len` carries the real width.

  > **This is the corrected RSA rule.** The first version specified double-base64
  > (`b64decode(b64decode(wire))` / `b64encode(b64encode(raw))`); that is WRONG and was a
  > round-trip (lossless-reconstruction) failure — see Fixes Applied #1.

- **secp256k1 legacy DER (tag 0x02), BTC (0x09), CRW (0x0A).** Wire is single base64 of the DER
  sig (`signer_ecdsa.py:136-138`; verify `:140-142`). Write `raw = b64decode(wire)`; Read
  `wire = b64encode(raw).decode()`. Raw = 64–72 B DER.

- **ED25519 (tag 0x03).** Wire is single base64 of the 64-byte raw sig (`signer_ed25519.py:154-156`;
  verify `:144-148`). Symmetric; raw = 64 B. (Post-fork ED25519 drops the pubkey but the
  *signature* wire form is unchanged — `signerfactory.py:237-247`.)

- **secp256r1 (tag 0x04).** Wire is single base64 of DER ECDSA-over-SHA256 (`signer_secp256r1.py`).
  Raw = 70–72 B.

- **ML-DSA 44/65/87 (tags 0x05/0x06/0x07).** Wire is single base64 of the lattice sig
  (`signer_mldsa.py`). Raw = 1312 / 3309 / 4627 B.

- **native multisig (tag 0x08).** Wire is single base64 of the blob
  `bytes([k]) || (idx u8, len u8, der_sig)*k` (`signer_multisig.py:124-138`). Write
  `raw = b64decode(wire)`; Read `wire = b64encode(raw).decode()`. The blob's INTERNAL structure
  is preserved **verbatim as opaque raw bytes** — we do NOT re-pack per component (that would risk
  reordering). `serialize_sigs`/`parse_sigs` already canonicalize on the consensus path (sorted by
  index, distinct, `signer_multisig.py:124-158`), and `verify_bis_signature_raw` re-base64s the raw
  blob and calls the existing verifier (`signer_multisig.py:194-196`), so the stored raw blob
  verifies directly.

- **secp256k1 recoverable hf2 (tag 0x40) — HEX, not base64.** Wire is the 65-byte compact
  `r(32)||s(32)||recovery_id(1)` as 130 lowercase hex chars (`signer_ecdsa.py:146-148`). Write
  `raw = bytes.fromhex(wire)` (must be exactly 65 B; the digester rejects otherwise,
  `signer_ecdsa.py:160-161`). Read `wire = raw.hex()`. Verification is
  `verify_bis_signature_recovered(raw.hex(), txid_hex, address)` (`signer_ecdsa.py:150-172`), fed
  the hex rebuilt from the stored bytes.

### 3. Reconstruction rule (lossless, reversible — round-trip proof)

Let `S` = the wire signature string (exactly as the legacy SQLite `signature` column / the wire
tuple holds it). Let `decode_t` / `encode_t` be the per-tag pair from §2. Then:

```
on WRITE:  raw = decode_t(S)            ; store blob = u8(tag) || u16le(len raw) || raw
on READ :  tag, raw = unpack(blob)      ; S' = encode_t(tag, raw)
```

Reversibility holds because every `decode_t` is a bijection on the set of **canonical** wire
strings:

- **base64 (tags 0x01–0x0A, multisig included):** `b64encode(b64decode(x)).decode() == x` for any
  `x` produced by Python's `b64encode` (Bismuth always emits canonical padded base64 with no
  newlines, via `sign_buffer_for_bis` across all signers). RSA is **one** such layer (single
  base64) — verified by `b64encode(b64decode(S)).decode() == S` on real mainnet rows across every
  era (h=2..1900002).
- **hex (tag 0x40):** `bytes.fromhex(x).hex() == x` for lowercase even-length `x`;
  `signer_ecdsa.py:148` emits exactly `.hex()`.

Therefore **`S' == S` byte-for-byte for every canonically-formed historical signature.** Because
`block_store`'s msgpack value is reconstructed field-by-field, `get_block(h)` (`block_store.py:133-139`,
which calls `_expand` `:90-99`) yields a row whose field [4] equals the original string. The
cross-check seams then hold byte-for-byte (C0 replay rule):

- `block_store.verify_against_sqlite` (`block_store.py:238-255`) asserts `get_block(h) == sqlite_rows`.
- `storage_backend.cross_check` (`storage_backend.py:130-142`) asserts
  `candidate.get_block(h) == reference rows` AND `candidate.block_hash(h) == rows[0][7]`.

Both compare the **reconstructed** form, so any non-canonical legacy string (should one exist)
surfaces as a cross-check failure and forces that row into the tag-0x00 fallback (§5) rather than
silently mutating consensus bytes.

For VERIFICATION specifically, **no rewrap is needed**: the consensus path calls
`SignerFactory.verify_bis_signature_raw(raw, raw_pubkey, buffer, address)` (`signerfactory.py:197-201`)
directly on the stored `raw`, and for tag 0x40 `verify_bis_signature_recovered(raw.hex(), txid, address)`
(`signer_ecdsa.py:150-172`). The buffer/txid is reconstructed by the frozen path
(`signature_buffer` `bismuth_serialize.py:23-29`, `tx_id_v2_s` `:159-163`).

**Block-hash pre-image stability.** The signature also feeds `_v2_tx_bytes`
(`bismuth_serialize.py:112-127`, fields 4–5 length-prefixed AS STORED). That function consumes the
tx in its **wire/string** form, and the block-hash dispatch (`block_hash_at`,
`bismuth_serialize.py:151-156`; gate `digest.py:624-626`) runs on rows produced by `get_block`
(i.e. AFTER `to_wire` rewrap). Since `S' == S`, `_v2_tx_bytes` sees identical bytes and
`block_hash_v2` is unchanged. The storage form of the signature can never perturb a hash.

### 4. Fork gate (by destination block height) and pre-fork byte-identity

The signature storage form is chosen by the **height of the block the row belongs to** (C0 —
destination height, never a global mode), mirroring `block_hash_at` / `tx_id_at`
(`bismuth_serialize.py:151-156`, `:166-175`):

```
post_fork = (node.fork_height is not None) and (height >= node.fork_height)
```

- **`node.fork_height is None` ⇒ 100% legacy.** Mainnet is `None` until on-chain lock-in
  (`digest.py:542-557`), so every current and historical block stores the signature as **legacy
  base64/hex TEXT verbatim** (a Python `str` in the msgpack tx list, tag-less) — byte-identical to
  today's store. **No migration, no re-encode of any existing LMDB value.** The 23 GB prod ledger
  is never force-rebuilt and never hot full-scanned (C0/C7); the pre-lock-in validation in §5 runs
  read-only from a snapshot copy.
- **`height >= fork_height` ⇒** the row's signature is stored as the §1 packed TRUE-BYTES blob
  (a `bytes` value with leading tag).

**Write gate (single site).** `block_store.put_blocks` (`block_store.py:102-115`) learns the
destination height (already the loop variable `height`) and the fork height (threaded from
`node.fork_height`). The verbatim `t = list(r[1:])` copy of field [4] becomes:

```
if fork_height is not None and height >= fork_height:
    t[4] = sigbytes.pack_from_wire(r[5], r[2])   # r[5]=signature, r[2]=address (12-field full row)
else:
    pass                                          # pre-fork: leave the legacy str untouched
```

(Full-row indices: `r[0]=block_height, r[1]=timestamp, r[2]=address, … r[5]=signature`; the stored
tx is `r[1:]`, so stored field [4] = full-row `r[5]`, and the dispatch address is full-row `r[2]`.)
`build_from_sqlite` / `verify_against_sqlite` (`block_store.py:219-255`) run with
`fork_height=None` and therefore take the legacy branch unchanged.

**Read rewrap (single site).** `_expand` (`block_store.py:90-99`) already re-expands the pubkey id;
it gains a sibling rewrap of field [4] **only when the stored value is a packed blob** — i.e. a
`bytes`/`memoryview` (legacy values are `str`). Concretely, alongside the pubkey re-expansion:

```
if isinstance(t[4], (bytes, bytearray, memoryview)):
    t[4] = sigbytes.to_wire(bytes(t[4]))
# else: legacy str passes through untouched  →  pre-fork byte-identity by construction
```

Pre-fork rows are stored as `str` and never re-encoded, so pre-fork byte-identity is **structural**.

**Dispatch / corruption cross-check on read.** `to_wire` reads the stored tag and (for non-0x00
tags) validates it against `SignerFactory.address_to_signer(address)` (and
`is_single_sig_ecdsa` for tag 0x40) for the row's address. A mismatch is a hard reject (store
corruption), since a Bismuth address commits to its key/scheme and cannot migrate scheme.

**C8 sentinel.** The `block_store` env carries `meta[b"fmt"] = b"\x04"`. An env copied from a
pre-Stage-4 node opens loud (refuses post-fork consensus reads until the operator confirms/rebuilds)
rather than silently mis-decoding a `bytes` blob that was actually legacy text — per C8.

### 5. Edge cases and fallbacks

- **Empty signature** (some coinbase/legacy rows): `raw = b""`, `sig_len = 0`, blob = `tag||0x0000`;
  read rebuilds `""`. The post-fork coinbase is compacted separately (doc/29 §2.D) and carries no
  signature.
- **Recoverable single-sig with non-65-byte hex (tag 0x40):** rejected at the digester
  (`signer_ecdsa.py:160-161`, gate `digest_tx.py:114`) before it can reach storage.
- **Non-canonical legacy base64/hex (theoretical):** detected by the **write-time self-check**
  `encode_t(tag, decode_t(S)) == S`. On failure, store with **tag 0x00 = opaque legacy text**: the
  original wire string is carried as `sig_bytes` (its UTF-8 encoding) and read back verbatim. This
  guarantees losslessness for any malformed historical row, at zero size benefit for that row only.
- **MANDATORY pre-lock-in validation (C0 replay rule, no-heavy-scan rule):** Before any
  `*_consensus` read flip, run the §3 round-trip self-check `encode_t(tag, decode_t(S)) == S` across
  a **full mainnet replay from a snapshot copy** (read-only; never a concurrent hot full-scan of the
  live 23 GB prod ledger — per the no-heavy-scans memory and C7). **Acceptance: ZERO RSA rows fall
  into the tag-0x00 opaque fallback** (any RSA fallback hit means `decode_t` is still wrong). Then
  `replay_verify` must report **0 mismatches at `fork_height=None`** AND **0 across a straddling
  (pre+post-fork) regnet chain**, and `storage_backend.cross_check` /
  `block_store.verify_against_sqlite` must stay byte-for-byte on the reconstructed forms — all AFTER
  the RSA single-base64 fix is in place.

### 6. Shielded ring signatures / key images — OUT OF SCOPE here

The shielded MLSAG/LSAG ring proofs, ring scalars, and key images
(`shieldedv1.py` `notes`/`kimg`/`kimg_h`) are **NOT** transaction `signature`-field bytes and are
**owned by the shielded Stage-4 domain spec**, not this one. They are flagged only as a pointer:

- The `kimg` / `kimg_h` **keys** are the consensus nullifier set (shielded double-spend prevention).
  Rewriting them from 66-char hex to raw 33-byte compressed points is a **consensus-relevant key-byte
  change** and MUST be validated in the shielded domain under its own state-root + cross-check (C7),
  not here.
- The signatures domain touches no nullifier/key-image set; this section is therefore
  consensus-safe in isolation.

This removes the prior cross-domain leak (the first version specified the shielded kimg/kimg_h key
rewrite inline) — see Fixes Applied #5.

### 7. Dispatch / gate site summary (file:line)

- **Write gate + pack:** `block_store.py:102-115` (`put_blocks`) → `sigbytes.pack_from_wire`.
- **Read rewrap:** `block_store.py:90-99` (`_expand`) → `sigbytes.to_wire` (bytes-only branch).
- **Scheme dispatch / tag derivation:** `signerfactory.py:120-134` (`address_to_signer`),
  `:204-208` (`is_single_sig_ecdsa` → tag 0x40).
- **Raw verify (no rewrap):** `signerfactory.py:197-201` (`verify_bis_signature_raw`); recoverable:
  `signer_ecdsa.py:150-172`.
- **Per-scheme decode/rewrap source of truth:** RSA `signer_rsa.py:124,142-156,168-171` (SINGLE
  base64); ECDSA `signer_ecdsa.py:119-148`; ED25519 `signer_ed25519.py:144-156`; secp256r1
  `signer_secp256r1.py`; ML-DSA `signer_mldsa.py`; multisig `signer_multisig.py:124-196`.
- **Fork-height source:** `digest.py:542-557` (detect/persist), read as `node.fork_height` (C0).
- **Cross-check that must stay byte-for-byte:** `storage_backend.py:130-142`,
  `block_store.py:238-255`.
- **Block-hash pre-image (sig AS STORED):** `bismuth_serialize.py:112-127` (`_v2_tx_bytes`),
  dispatch `:151-156`, gate `digest.py:624-626`.


**Savings**

| field | legacy form | legacy bytes | true-bytes form | new bytes | saving % |
|---|---|---|---|---|---|
| RSA-1024 sig (early mainnet, 1024-bit key) | SINGLE base64 of 128B raw | ~172 | tag(1)+u16(2)+raw(128) | 131 | ~24% |
| RSA-4096 sig (later mainnet, 4096-bit key) | SINGLE base64 of 512B raw | ~684 | tag(1)+u16(2)+raw(512) | 515 | ~25% |
| secp256k1 legacy DER sig | base64 of ~71B DER | ~96 | tag(1)+u16(2)+raw(71) | 74 | ~23% |
| secp256k1 recoverable (hf2, tag 0x40) | 130 lc hex of 65B compact | 130 | tag(1)+u16(2)+raw(65) | 68 | ~48% |
| ED25519 sig | base64 of 64B | ~88 | tag(1)+u16(2)+raw(64) | 67 | ~24% |
| secp256r1 (P-256) sig | base64 of ~71B DER | ~96 | tag(1)+u16(2)+raw(71) | 74 | ~23% |
| ML-DSA-44 sig | base64 of 1312B | ~1752 | tag(1)+u16(2)+raw(1312) | 1315 | ~25% |
| ML-DSA-65 sig | base64 of 3309B | ~4412 | tag(1)+u16(2)+raw(3309) | 3312 | ~25% |
| ML-DSA-87 sig | base64 of 4627B | ~6172 | tag(1)+u16(2)+raw(4627) | 4630 | ~25% |
| native multisig (M=2) blob | base64 of ~146B (k + 2×(idx,len,71B DER)) | ~196 | tag(1)+u16(2)+raw(146) | 149 | ~24% |
| BTC / CRW (test) sig | base64 of ~71B | ~96 | tag(1)+u16(2)+raw(71) | 74 | ~23% |


**Adversarial fixes folded in:**
- FATAL RSA decode/rewrap fixed: changed tag 0x01 from DOUBLE base64 (b64decode(b64decode(S)) / b64encode(b64encode(raw))) to SINGLE base64 — decode_t(S)=b64decode(S), encode_t(raw)=b64encode(raw).decode(). This matches signer_rsa.py:124 (one b64decode on verify), signer_rsa.py:168-171 (one b64encode on sign), and signer_rsa.py:142-156 (verify_bis_signature_raw takes the singly-decoded raw). Verified empirically by read-only single-row sampling of static/ledger.db: b64encode(b64decode(S)).decode()==S for h=2,3,4,500000,1900000,1900001,1900002; a SECOND b64decode either raises binascii.Error or yields non-deterministic garbage, proving the column is single, not double, base64. The round-trip (lossless-reconstruction) invariant now holds for RSA.
- Savings table RSA rows corrected: removed the false 'double-base64 of 512B (b64(b64(raw))) ~912 bytes / ~43%' row and replaced with TWO real rows reflecting both mainnet eras — RSA-1024 (~172 wire → tag(1)+u16(2)+raw(128)=131, ~24%) and RSA-4096 (~684 wire → tag(1)+u16(2)+raw(512)=515, ~25%). No single 'RSA-4096' size assumption remains.
- Variable RSA raw length documented explicitly: §1.1 and §2 state RSA raw is 128B (legacy 1024-bit keys) or 512B (4096-bit keys), both present on mainnet, carried by the dynamic u16 sig_len. A Risk and Open Question pin this so no reviewer re-introduces a fixed-512 assumption.
- Mandatory pre-lock-in validation added (§5): run the §3 write-time self-check encode_t(decode_t(S))==S across a FULL mainnet replay from a SNAPSHOT COPY (read-only, never a hot full-scan of the live 23GB prod ledger per the no-heavy-scans rule and C7), with explicit ACCEPTANCE that ZERO RSA rows fall into the tag-0x00 opaque fallback; then replay_verify 0-mismatch at fork_height=None AND across a straddling chain, plus storage_backend.cross_check / block_store.verify_against_sqlite byte-for-byte — all AFTER the RSA single-base64 fix, before approval.
- Shielded ring/key-image rewrite moved fully OUT OF SCOPE (§6): the prior version inline-rewrote the kimg/kimg_h consensus nullifier-set KEYS (66-hex → raw 33B point); this revision removes that and states it is owned by the shielded Stage-4 domain spec (validated there under its own state-root/cross-check per C7), referenced here only as a pointer. The signatures domain touches no nullifier/key-image set and is consensus-safe in isolation.
- Aligned to SHARED CONVENTIONS: block_store value is msgpack per C6 (not JSON, not raw — the sig blob lives inside the msgpack tx list); Codec.hkey 8B BE key per C1; every blake2b cites digest_size per C2 (block_store pubkey-dedup pk = blake2b digest_size=32, block_store.py:79); A-hex ban per C0 (sig_bytes are TRUE bytes, the recoverable hex is a frozen consensus wire form not a storage choice); C8 fmt sentinel (meta[b'fmt']=b'\x04') added so a pre-Stage-4 env copy refuses post-fork reads instead of mis-decoding str-vs-bytes.


**Implementation checklist (residual review items to satisfy before wiring this domain):**
- [ ] Resolve the msgpack/JSON-fallback contradiction for the packed bytes signature blob. Either: (a) make msgpack a HARD requirement for any node that writes/reads a post-fork block_store (drop the JSON fallback for block_store specifically, and have the C8 fmt sentinel / open refuse to serve when msgpack is absent and fmt>=\x04), or (b) give the JSON-fallback path a bytes-safe transport that is NOT hex/base64-in-text inside a JSON value (which would violate the A-hex ban and the savings claim anyway). Option (a) is cleanest and consistent with C6/C8. The spec must state explicitly that the post-fork packed-bytes sig blob is incompatible with the JSON fallback engine and pin the mitigation; as written, a post-fork put_blocks crashes with TypeError on a no-msgpack node.
- [ ] State explicitly in §4 that put_blocks gains a fork_height parameter DEFAULTING to None, so build_from_sqlite/verify_against_sqlite (block_store.py:230,232,248 — no node ref) take the legacy str branch unchanged. Also specify the behavior of rebuilding LMDB from a SQLite ledger that already contains post-fork blocks: with fork_height=None the rebuild stores post-fork rows as legacy str (correct/lossless but un-compacted and possibly inconsistent with a C8 fmt=\x04 sentinel written by a normally-synced node). Pin whether build_from_sqlite must be passed the real fork_height to keep the post-fork region in packed form on rebuild, so a rebuilt env and a synced env produce byte-identical block_store values.

---

## 2. Public-key storage (drop / dedup / raw)  `[pubkeys]`

## hf2 Stage-4 TRUE-BYTES Public-Key Storage (`pk` / `pkr` drop + dedup + raw)

> Obeys the **SHARED CROSS-DOMAIN CONVENTIONS** verbatim. Where this domain touches
> a shared encoding, the shared section wins: txid raw form (C3), `blake2b`
> digest_size pinning (C2), the raw-bytes-no-codec rule for `pkr` (C6), per-env /
> snapshot / format-sentinel rules (C7/C8), and the single fork signal +
> destination-height gate (C0).

### Scope

This section governs how the `public_key` field (field index 5 of the canonical
8-field tx, `bismuth_serialize.py:19-20`; index 5 of the stored 11-field row after
`block_height` is dropped, `block_store.py:48-50` `_PK = 5`) is stored in the LMDB
`block_store` for blocks whose **destination height is `>= node.fork_height`**. It
is a **storage-layer** change only: the frozen consensus pre-images
(`signature_buffer` / `tx_id` / `signed_message` / `block_hash`,
`bismuth_serialize.py:23-55`, and the v2 siblings `signature_buffer_v2` /
`tx_id_v2` / `_v2_tx_bytes` / `block_hash_v2`, `bismuth_serialize.py:77-147`) are
NOT mutated. The 12-field ledger rows reconstructed on read are byte-identical to
the SQLite rows (proven by `verify_against_sqlite` `block_store.py:238-255` and
`storage_backend.cross_check` `storage_backend.py:130`).

Three storage classes, dispatched **by the sender-address base58/hex string** using
the *exact* predicates the verifier uses (single-sourced — see "Single-sourced DROP
predicate"):

| Class | Schemes | Stored pubkey (v2 region) |
|---|---|---|
| **DROP** | secp256k1 single-sig, ED25519 single-sig | nothing in `pk`/`pkr`; per-tx field is the 1-byte tag `0x00` (reconstructs `""`) |
| **DEDUP-RAW** | RSA, ML-DSA-44/65/87, secp256r1, multisig redeem | store-once in `pkr` as **base64-decoded raw bytes**; per-tx field is `tag 0x01 + u64 id` |
| **DEDUP-VERBATIM** | any DEDUP-class field that FAILS the canonical-base64 guard | store-once in `pkr` as the **exact stored field bytes** (no decode); per-tx field is `tag 0x02 + u64 id` |

The DEDUP-VERBATIM class is the lossless safety net mandated by the adversarial
review (see "Hard canonical-base64 guard"). It is never normally exercised by
node-produced txs, but its existence is what lets this section *prove* lossless
reconstruction instead of merely asserting it.

---

### CRITICAL CORRECTION — what the wire `public_key` field actually is

The first version assumed the DEDUP value is "base64 of raw DER ~550 bytes". The
source disproves this for the dominant case:

- **RSA** wire `public_key` = `b64encode(public_key_PEM_TEXT)`
  (`signer_rsa.py:168-171` produces b64 of the PEM; `verify_bis_signature` does
  `b64decode(public_key).decode('utf-8')` → PEM, `signer_rsa.py:120`). The decoded
  bytes are the **PEM ASCII text** (`-----BEGIN PUBLIC KEY-----\n…\n-----END…-----`),
  whose length is one of `271` or `799` chars (`signer_rsa.py:87`), NOT raw DER. So
  the b64-decoded raw value for a 4096-bit RSA key is **~799 PEM bytes**, and its
  base64 wire form is **~1068 chars** (matching `PUBKEY_FIELD_MAX = 1068`,
  `signer_multisig.py:51`), capped by `MAX_TX_PUBKEY_LEN = 4096` (`essentials.py:26`).
- **ML-DSA / secp256r1 / multisig redeem** wire `public_key` = `b64encode(raw_bytes)`
  of the genuine key/redeem bytes (`signer_mldsa.py`, `signer_secp256r1.py`,
  `signer_multisig.py:12-13` redeem = `bytes([M,N]) || pub₁(33)…pubₙ(33)`).

The Stage-4 change is identical in spirit regardless: **base64-decode the stored
field on write so `pkr` holds the genuine decoded bytes (PEM text bytes for RSA, raw
key bytes for the rest), and base64-encode on read.** The decoded value is true
bytes in the LMDB value — no A-hex/b64-in-text regression (C0 A-hex ban). The
savings table below is corrected to these real sizes.

---

### Why the value is base64-as-bytes today (the regression we remove)

`block_store.put_blocks` passes the wire `public_key` straight into `_pubkey_id`,
which does `pk.encode()` (`block_store.py:75`) — it stores the **UTF-8 bytes of the
base64 string**. So today's `pkr` value is base64-as-bytes (1.33× the decoded key),
and `_expand` calls `pkb.decode()` (`block_store.py:97`) to hand the base64 text
back. That is the A-hex-class regression in miniature: a binary store holding an
expanded text encoding. The fix is base64-**decode** on write and base64-**encode**
on read, so the LMDB value is the genuine decoded key bytes (C6: `pkr` is in the
"Raw bytes — no codec" tier; A-hex ban applies).

---

### A. DROP class — recoverable single-sig, ZERO pubkey bytes

For a post-fork tx whose sender address is single-sig secp256k1 or ED25519, the
public key is fully recoverable from `(content-txid, signature)` via ecrecover
(`signer_ecdsa.py` `verify_bis_signature_recovered`) or directly from the address
(`SignerED25519.public_key_from_address`). The verifier **rejects** a non-empty
`public_key` for these post-fork (`signerfactory.py:231-232` secp256k1,
`signerfactory.py:241-242` ED25519), so the canonical wire form has an empty
`public_key`. Storage mirrors that exactly.

Stored per-tx public_key field (the value at `t[i][5]` inside the msgpack block
record). To keep the reader unambiguous, the v2 region stores `t[i][5]` as a
**2-element msgpack list `[tag, payload]`** (see "Per-tx record shape" below); for
DROP the payload is absent:

```
field         | type | width | value
--------------+------+-------+------------------------------------------
pk_class tag  | u8   | 1     | 0x00  (DROP — reconstruct empty string "")
              |      |       | (msgpack element: [0])
```

No `pk`/`pkr` rows. Total on-disk for a DROP tx's pubkey: **1 tag byte** inside the
already-present msgpack list element. Pre-hf2 these schemes carried a real ~44–85 B
base64 pubkey value; post-hf2 the wire form is empty by the verifier rule, so the
1-byte tag is the entire cost.

**Single-sourced DROP predicate (REQUIRED FIX).** The writer decides DROP **only**
by calling the *same* `SignerFactory.is_single_sig_ecdsa(addr)` /
`SignerFactory.is_single_sig_ed25519(addr)` (`signerfactory.py:204-215`) that the
verifier's `verify_tx_signature` calls (`signerfactory.py:228, 237`). The address it
passes is the stored sender-address string `r[2]` (12-field row index 2 =
`address`; equivalently `t[1]` in the 11-field stored row). There is **no second
classifier** and no inlined regex in `block_store`; if the consensus predicate ever
changes, the writer changes with it automatically. This makes the verifier-reject
and writer-reject impossible to drift.

**Reject rule (write path).** If a DROP-class tx arrives with a non-empty
`public_key`, the writer raises `ValueError("post-fork DROP-class tx must carry an
empty public key")` — the exact invariant `signerfactory.py:231,241` enforce, read
through the same predicate, so storage can never disagree with consensus.

---

### B. DEDUP-RAW class — store-once decoded key, tag + 8-byte id

RSA / ML-DSA / secp256r1 keys and the multisig redeem are NOT recoverable and are
carried on the wire (base64). They are 1:1 with the sender address and repeat on
every spend, so the existing `block_store` dedup (`block_store.py:71-88`) already
stores each distinct key once and references it by an 8-byte id. Stage-4 keeps the
dedup mechanism and changes only the **value encoding** (base64-as-bytes → decoded
bytes) and the **per-tx record shape** (bare int → tagged list).

`pk` sub-db (content-hash → id):

```
key   | type                       | width | value
------+----------------------------+-------+--------------------------------
hkey  | blake2b digest_size=32 (C2)| 32    | dedup id, Codec.hkey(id) = >Q (C1)
      | over the STORED v2 field    |       | 8-byte BE uint64
```

> **Content-hash basis (REQUIRED FIX — clean rebuild).** The `pk` hkey is taken over
> the **decoded raw bytes** for tag-`0x01` entries and over the **verbatim field
> bytes** for tag-`0x02` entries — i.e. over whatever bytes land in `pkr`, so the
> hash always identifies the value actually stored. The legacy region keeps hashing
> the base64-as-bytes value (current behavior, `block_store.py:79`). Because the
> two bases differ, an env may **never** mix legacy-hashed and v2-hashed entries for
> the same logical key. This is enforced structurally by the **clean rebuild at the
> fork boundary** (see "Migration: clean rebuild only") and the **format-version
> sentinel** (C8): there is no in-place hash-basis migration. The hkey is internal
> to the store — it is never reconstructed onto a row and never crosses a consensus
> boundary (`block_store.py:76-79` note) — so this choice has zero consensus impact;
> it only has to be self-consistent within one env.

`pkr` sub-db (id → stored value) — the byte-layout change:

```
key   | type                | width | value
------+---------------------+-------+----------------------------------------
id    | Codec.hkey(id) = >Q | 8     | DECODED key bytes: base64.b64decode(field,
      | 8-byte BE uint64(C1) |       | validate=True). True bytes, no codec (C6).
      |                     |       | RSA: ~799 PEM bytes. ML-DSA-87: 2592 B. etc.
```

Per-tx public_key field stored in `blocks[height].t[]` (msgpack element
`[tag, id]`):

```
field         | type   | width | value
--------------+--------+-------+----------------------------------------------
pk_class tag  | u8     | 1     | 0x01  (DEDUP-RAW)
dedup id      | u64    | (msgpack int) | id into pkr (Codec.hkey(id) on store)
              |        |       | (msgpack element: [1, id])
```

**Multisig** is DEDUP-RAW: the wire `public_key` is `b64encode(redeem)` where
`redeem = bytes([M,N]) || pub₁(33)…pubₙ(33)` (`signer_multisig.py:12-13, 67-83`,
BIP67-sorted). It is base64-decoded to the raw redeem and stored in `pkr` exactly
like an RSA key. No per-constituent dedup — the redeem is the atomic unit the wire
carries and the address commits to (`redeem_to_address`), so storing it whole is
correct and already deduped across repeat spends from the same vault. (Deliberate
non-optimization; noted in risks.)

---

### Hard canonical-base64 guard + DEDUP-VERBATIM fallback (REQUIRED FIX — closes the lossless hole)

The lossless break the review found is real and proven: `base64.b64encode(base64.
b64decode(field))` equals `field` **only** for canonically-encoded base64. The
codebase has live evidence of non-canonical pubkey content — `mempool.py:359-361`
strips a literal `"b'"` prefix ("Binary content instead of str - leftover from
legacy code?"). Empirically (verified this session):

- a field with an **embedded newline/whitespace** (e.g. PEM-shaped) decodes under
  the lenient default but `b64encode(decode(field)) != field` — a **silent**
  divergence;
- a field with a **`b'` cruft prefix** decodes to wrong bytes — silent divergence;
- padding/length errors raise (loud, acceptable).

Because `block_hash_v2`'s `_v2_tx_bytes` length-prefixes the **reconstructed**
`public_key` string (`bismuth_serialize.py:125`), any silent divergence on a
post-fork tx changes the v2 block hash → **consensus fork**. The guard makes this
impossible:

**Write-path guard (in `_pubkey_id`, v2 region only), applied to the TRUNCATED
stored field** (the field already cut to `MAX_TX_PUBKEY_LEN=4096` by
`digest.py:265` / `mempool.py:358`, which is the exact bytes `block_store` receives):

```python
field = stored_pubkey_str          # == str(tx[5])[:MAX_TX_PUBKEY_LEN], already truncated
b = field.encode("ascii")          # the stored field as bytes
try:
    raw = base64.b64decode(b, validate=True)        # validate=True => any non-alphabet
    canonical = (base64.b64encode(raw) == b)        # char (whitespace, b', etc.) raises
except (binascii.Error, ValueError):
    canonical = False
if canonical:
    tag, value = 0x01, raw         # DEDUP-RAW: store decoded bytes
else:
    tag, value = 0x02, b           # DEDUP-VERBATIM: store the field bytes UNMODIFIED
```

`validate=True` turns the embedded-whitespace and `b'`-cruft cases into a raised
error (caught → `canonical = False`), and the explicit `b64encode(raw) == b`
re-check catches any residual non-canonical padding the decoder tolerated. Either
branch is **provably lossless** (proof below). The guard never *rejects* a tx — it
routes the rare non-canonical field to the verbatim branch — so it adds no new
consensus reject path and cannot fork on a relayed tx.

**Read-path reconstruction (`_expand`, `block_store.py:90-99`), branch on tag:**

```
[0]      (DROP):           t[5] := ""                                   # empty wire form
[1, id]  (DEDUP-RAW):      raw := pkr[Codec.hkey(id)]
                           t[5] := base64.b64encode(raw).decode("ascii")
[2, id]  (DEDUP-VERBATIM): t[5] := pkr[Codec.hkey(id)].decode("ascii") # field bytes, verbatim
bare int (legacy region):  t[5] := pkr[Codec.hkey(int)].decode()       # today's exact behavior
```

---

### Reconstruction rule (C-RECON) — lossless, round-trip proof over the TRUNCATED stored field

The proof is stated over `db_public_key_b64encoded = str(tx[5])[:MAX_TX_PUBKEY_LEN]`
(`digest.py:265`) — the exact bytes the store persists, **not** the pre-truncation
wire field (REQUIRED FIX). For every tag:

- **DROP (`0x00`):** post-fork wire `public_key` for these schemes is `""` by the
  verifier rule (`signerfactory.py:231,241`); reconstructing `""` is exact.
  Verification needs no stored pubkey (ecrecover / address-embedded). Lossless.
- **DEDUP-RAW (`0x01`):** entered **iff** `base64.b64encode(base64.b64decode(field,
  validate=True)) == field`. Therefore `base64.b64encode(pkr[id]) == field` by
  construction → reconstructed string == stored field byte-for-byte. Lossless.
- **DEDUP-VERBATIM (`0x02`):** `pkr[id]` is the field bytes copied verbatim; reconstruction
  is `pkr[id].decode("ascii")` == field. Lossless by identity, no base64 involved.
- **Legacy region (bare int):** unchanged; `pkr[id].decode()` returns the original
  base64-as-stored string. Lossless (today's behavior, untouched).

In all cases the reconstructed `t[5]` equals the stored field, so the 12-field row
equals the SQLite row and the v2 block-hash input (`_v2_tx_bytes`,
`bismuth_serialize.py:112-127`) sees the same bytes it sees today. **No consensus
function is modified.** `signature_buffer`/`signature_buffer_v2` exclude
`public_key` entirely; `tx_id`/`tx_id_v2` exclude it; only `_v2_tx_bytes` /
`block_hash_v2` length-prefix the reconstructed string and they receive the
identical bytes. `cross_check` (`storage_backend.py:130`) and
`verify_against_sqlite` (`block_store.py:238-255`) assert this block-for-block and
must stay 0-mismatch across a straddling chain.

---

### Per-tx record shape (REQUIRED FIX — uniform tagged form, no dual-shape reader)

In the v2 region `t[i][5]` is **always** a msgpack list: `[0]` (DROP),
`[1, id]` (DEDUP-RAW), or `[2, id]` (DEDUP-VERBATIM). In the legacy region
`t[i][5]` stays a **bare msgpack int** (today's exact behavior, `block_store.py:112`).
`_expand` distinguishes them structurally: `isinstance(t[5], (list, tuple))` ⇒ v2
tagged; `isinstance(t[5], int)` ⇒ legacy bare-int. Because the two regions never
share an env after the clean rebuild (next section), the reader never has to guess.

There is **no dual-shape reader for already-written v2 LMDB**: any pre-existing
regnet post-fork LMDB written on the old bare-int v2 shape is re-imported by the
clean rebuild, not patched in place.

---

### Migration: clean rebuild only (REQUIRED FIX) + format-version sentinel (C8)

The SQLite→LMDB block_store is **post-fork-only** (`storage_backend.select` gates on
`fork_height is not None and last_block >= fork_height`, `storage_backend.py:145-158`),
so there is no pre-fork data to migrate in place. Stage-4 mandates:

- **Clean rebuild at the fork boundary.** The v2 region is (re)populated by a single
  `build_from_sqlite` / rebuild pass (`block_store.py:219-235`) so the `pk`
  content-hash basis (decoded-bytes vs base64-as-bytes) and the `t[i][5]` record
  shape (tagged vs bare-int) are **uniform within one env**. No in-place hash-basis
  or shape migration is ever performed.
- **Format-version sentinel (C8).** `block_store` gains a `meta` sub-db (today it
  has only `blocks`/`hashes`/`pk`/`pkr`, `block_store.py:55`). `meta[b"fmt"] =
  b"\x04"` marks a Stage-4 env. On open under Stage-4 code: if `fmt` is absent or
  `< 0x04` (e.g. an operator-copied pre-Stage-4 env with base64-as-bytes `pkr`
  values and bare-int `t[5]`), the store **refuses to serve consensus reads until
  rebuilt** (canonical-store rule, C8) rather than silently base64-encoding
  already-base64 values or `_hk()`-ing a list. The sentinel is written atomically
  with the rebuild's first commit. This makes the old-encoding mismatch loud and
  self-healing instead of a silent correctness hole.
- **Rebuild from a snapshot, never a hot full-scan of prod** (C7 / no-heavy-scans
  memory): the 23 GB prod ledger is never force-rebuilt and never hot full-scanned.

---

### Fork gate (by destination height) and pre-fork byte-identity

The encoding is chosen by the height of the block the bytes belong to, never by a
global mode (C0):

- `block_store` is opened/written **post-fork** (canonical store at/after
  `fork_height`; pre-fork the SQLite ledger is source of truth,
  `storage_backend.select` `storage_backend.py:145-158`).
- Within `put_blocks`, the class/encoding is decided **per stored block's height**.
  For any height `< fork_height` (back-filled history copied into LMDB) the writer
  takes the **legacy branch**: store the base64 text as-is under the
  bare-integer-id dedup (today's exact behavior, value = `pk.encode()`), **no tag**,
  hash basis = base64-as-bytes. For `>= fork_height` it takes the DROP / DEDUP-RAW /
  DEDUP-VERBATIM branch.
- `fork_height is None` ⇒ legacy path for 100% of blocks by construction (mainnet
  today). Every historical block re-serializes byte-identically regardless of node
  config, because its destination height alone selects the branch.

**Single fork signal:** every gate reads `node.fork_height` (C0). No new consensus
signal; `amounts.LEDGER_INTEGER` (the orthogonal storage flag) is not consulted.

---

### Why the consensus address→key registry was REJECTED, and store-dedup is the right layer

A "pubkey-by-reference at the consensus layer" (the wire tx omits the key after the
sender's first appearance; a consensus-replicated `address → key` registry supplies
it at validation) was rejected decisively (doc/29 §2.C) for reasons this section must
not reintroduce:

1. **Intra-block tx-ordering dependency.** Whether tx N validates would depend on
   whether tx N−k (same sender, earlier in the *same* block) registered the key
   first — breaking Bismuth's order-independence within a block and complicating
   every reorg / snapshot / parallel-validation path.
2. **Reorg/snapshot-fragile consensus state** + a new state-dependent reject
   ("unknown key reference") — pure new attack surface.
3. **It buys nothing the store doesn't give losslessly** — the only keys it would
   compress are RSA/ML-DSA repeats, which `pk`/`pkr` dedup already collapses to one
   copy + an 8-byte id, with zero consensus impact.

Store-level dedup is correct precisely because it is **invisible to consensus**: it
sits behind the frozen serialization boundary (`block_store.py:11-15, 28-31`),
reconstructs the exact wire field on read, and is proven byte-identical by
`cross_check`/`verify_against_sqlite`. It has **no ordering dependency** (id is
content-addressed by blake2b over the stored value, assigned `txn.count(pk)` within
the write txn) and rolls back naturally with `block_store.rollback`
(`block_store.py:120-130`) since ids are only referenced by the blocks containing
them.

---

### Dispatch / gate sites that change

- `block_store.py:71-88` `_pubkey_id` — add per-height class dispatch. v2 region:
  call `SignerFactory.is_single_sig_ecdsa`/`is_single_sig_ed25519` on the sender
  address → DROP (raise on non-empty pubkey); else run the **canonical-base64
  guard** → DEDUP-RAW (decode, store raw, hash over raw) or DEDUP-VERBATIM (store
  field bytes, hash over field bytes). Legacy region: keep the current `pk.encode()`
  path verbatim (hash over base64-as-bytes, bare-int id).
- `block_store.py:90-99` `_expand` — branch on the stored tag: `[0]` → `""`;
  `[1,id]` → `base64.b64encode(pkr[id]).decode()`; `[2,id]` → `pkr[id].decode()`;
  bare int → legacy `pkr[id].decode()`.
- `block_store.py:102-118` `put_blocks`/`put_block` — thread the destination
  `height` into `_pubkey_id` so the class is chosen by `height >= fork_height`;
  enforce the empty-pubkey reject for DROP-class senders via the single-sourced
  predicate.
- `block_store.py:55` `open_store` dbs — add `"meta"` for the C8 `fmt` sentinel.
- `block_store.py:48-50` `_PK` — unchanged (index 5).
- No change to `bismuth_serialize.py` (consensus frozen), `signerfactory` predicates
  (reused, not modified), `storage_backend.select`, or any signer.


**Savings**

| field | legacy form | legacy bytes | true-bytes form | new bytes | saving % |
|---|---|---|---|---|---|
| secp256k1 single-sig pubkey (per tx) | base64 of 33B compressed key, stored as b64-bytes (deduped value + per-tx ref) | ~45 (b64 text value) | DROP — `[0]` 1-byte tag, no `pk`/`pkr` row | 1 | ~98% |
| ED25519 pubkey (per tx) | base64 of 32B key, stored as b64-bytes | ~44 | DROP — `[0]` 1-byte tag, no `pk`/`pkr` row | 1 | ~98% |
| RSA-4096 pubkey (deduped value in `pkr`, once) | b64(PEM text) stored as UTF-8 bytes (~1068 b64 chars) | ~1068 | base64-decoded **PEM text bytes** (799-char PEM) | 799 | ~25% |
| RSA-4096 pubkey (per-tx reference) | bare msgpack int id | ~3-9 | `[1,id]` tag+id msgpack list | ~4-10 | ~ -1B (offset by value win) |
| ML-DSA-44 pubkey (deduped value, once) | base64 of 1184B, stored as b64-bytes | ~1580 | raw 1184B (b64-decoded) | 1184 | ~25% |
| ML-DSA-65 pubkey (deduped value, once) | base64 of 1952B, stored as b64-bytes | ~2604 | raw 1952B (b64-decoded) | 1952 | ~25% |
| ML-DSA-87 pubkey (deduped value, once) | base64 of 2592B, stored as b64-bytes | ~3456 | raw 2592B (b64-decoded) | 2592 | ~25% |
| secp256r1 pubkey (deduped value, once) | base64 of 65B, stored as b64-bytes | ~88 | raw 65B (b64-decoded) | 65 | ~26% |
| multisig redeem (deduped value, once; 2-of-3 = 2 + 99B = 101B) | base64 of ~101B, stored as b64-bytes | ~136 | raw ~101B redeem (b64-decoded) | 101 | ~26% |
| DEDUP-VERBATIM (rare non-canonical field, once) | base64-as-bytes (legacy) | =field | exact field bytes (no decode), `[2,id]` | =field | 0% (lossless safety net; no regression vs legacy) |


**Adversarial fixes folded in:**
- LOSSLESS HOLE (the one real break): Added a HARD canonical-base64 write-path guard in _pubkey_id (v2 region), applied to the TRUNCATED stored field: base64.b64decode(field, validate=True) AND require base64.b64encode(raw) == field. validate=True turns embedded-whitespace and the b'-cruft case (mempool.py:359-361 evidence) into a raised error; the explicit re-encode check catches any residual non-canonical padding. Fields that pass -> DEDUP-RAW (decoded bytes). Fields that FAIL -> new DEDUP-VERBATIM class (tag 0x02) that stores the exact field bytes unmodified and bypasses the decode/encode round-trip, reconstructing byte-for-byte. Both branches are provably lossless, so b64encode(b64decode(reconstructed)) can no longer diverge and block_hash_v2 (which length-prefixes the reconstructed pubkey at bismuth_serialize.py:125) can no longer fork. Verified empirically this session that validate=True raises on embedded newline / b' prefix and that the lenient decoder otherwise diverges silently.
- Single-source the DROP empty-pubkey predicate: the block_store writer now calls the EXACT is_single_sig_ecdsa / is_single_sig_ed25519 (signerfactory.py:204-215) that verify_tx_signature calls (signerfactory.py:228,237), passing the stored sender-address string. No inlined regex / second classifier in block_store. Writer-reject and verifier-reject (signerfactory.py:231,241) therefore cannot drift; spec forbids any future inlining.
- Mandate a clean rebuild_from_sqlite at the fork boundary (no in-place migration): documented that block_store is post-fork-only so there is no pre-fork data to migrate; the v2 region is (re)populated by a single build_from_sqlite pass so the pk content-hash basis (decoded-bytes for RAW, field-bytes for VERBATIM vs legacy base64-as-bytes) and the t[i][5] record shape (tagged list vs bare int) are uniform within one env. Added the C8 format-version sentinel meta[b'fmt']=b'\x04' (block_store gains a meta sub-db): an absent/lower fmt makes the canonical store REFUSE consensus reads until rebuilt, so a copied pre-Stage-4 env fails loud instead of silently base64-encoding already-base64 values or _hk()-ing a list. Any pre-existing regnet post-fork LMDB on the old bare-int shape must be re-imported (no dual-shape reader).
- State and characterization-lock the round-trip proof over the TRUNCATED stored public_key: the C-RECON proof is now stated over db_public_key_b64encoded = str(tx[5])[:MAX_TX_PUBKEY_LEN] (digest.py:265 / mempool.py:358, MAX_TX_PUBKEY_LEN=4096 essentials.py:26), not the pre-truncation wire field. The fork_gating_note mandates replay_verify + storage_backend cross_check 0-mismatch on a straddling chain containing at least one RSA, one ML-DSA, one multisig, one DROP, and one DEDUP-VERBATIM tx, asserting block_store.block_hash(height) == block_hash_at over reconstructed rows for every post-fork height.
- CORRECTED the byte forms the original got wrong: RSA wire public_key is b64encode(PEM TEXT) (signer_rsa.py:168-171,120), so the decoded value is ~799 PEM bytes (signer_rsa.py:87), NOT ~550 DER bytes; the legacy b64-as-bytes value is ~1068 chars (PUBKEY_FIELD_MAX, signer_multisig.py:51). The savings table is corrected to real sizes and the per-tx reference row no longer claims a spurious -12% (tag+id is ~+1 byte over a bare msgpack int). Added an open-question forbidding a DER micro-optimization that would change the reconstructed PEM column and fork block_hash_v2.


**Implementation checklist (residual review items to satisfy before wiring this domain):**
- [ ] Replace the ascii codec with the legacy UTF-8 default to preserve byte-identity with the existing block_store path: write the field as field.encode() (UTF-8, matching block_store.py:75) and reconstruct DEDUP-RAW/VERBATIM with pkr[id].decode() (UTF-8, matching block_store.py:97). Additionally widen the guard's except to include UnicodeError/UnicodeEncodeError (or operate on the field bytes obtained via .encode() once, before b64decode) so a non-ASCII/non-canonical field is routed to DEDUP-VERBATIM (stored verbatim, reconstructed verbatim) instead of crashing the writer. Add a straddling-chain replay_verify case with a deliberately non-ASCII / b'-cruft public_key to lock this.
- [ ] Correct the spec text: remove the false 'the two regions never share an env after the clean rebuild' / 'eliminated structurally' justification (append_block writes pre-fork bare-int and post-fork tagged records into the same env during forward sync). State explicitly that a single block_store env legitimately MIXES legacy bare-int t[5] with v2 tagged-list t[5] and MIXES pk hash bases, and that correctness comes from (a) the per-tx isinstance(t[5],(list,tuple)) dispatch in _expand and (b) content-addressed dedup making cross-basis entries independent (lossless, at worst reduced dedup). The C8 fmt sentinel still guards an OLD-format (pre-Stage-4) env, but it does not and cannot separate pre/post-fork regions within a Stage-4 env.
- [ ] Keep the replay_verify recompute requirement explicit and binding (block_store.block_hash(height) == block_hash_at over the RECONSTRUCTED rows for every post-fork height) and do not let the section imply cross_check alone proves block_hash_v2 reproduction - cross_check only compares the stored hash value against the row's column.

---

## 3. Core tx record fields  `[tx-fields]`

## Stage-4 TRUE-BYTES LMDB record — core tx non-signature fields (`tx-fields`)

### Scope and the storage/consensus split

This section specifies the on-disk byte layout of a stored transaction's **non-signature, non-pubkey** fields inside `block_store`'s `blocks` sub-db (`block_store.py:57,114`). The fields owned here are the six canonical tx fields that are NOT crypto material — `timestamp`, `address`, `recipient`, `amount`, `operation`, `openfield` — **plus** the two **ledger-only** money fields `fee` and `reward`. Signature and `public_key` are out of scope: they keep the existing pk-dedup id slot (`block_store.py:71-99`) and the signers domain owns their bytes; we leave their positions in the row untouched.

**Load-bearing invariant (obeys C0).** Storage form is independent of the consensus pre-image form. The frozen consensus functions `signature_buffer`/`tx_id`/`signed_message`/`block_hash` (`bismuth_serialize.py:23-55`) and their v2 siblings `signature_buffer_v2`/`tx_id_v2`/`block_hash_v2`/`_v2_tx_bytes` (`bismuth_serialize.py:77-147`) are **NOT mutated**. Crucially, every pre-image is computed over the **8-field tuple** `(timestamp, address, recipient, amount, signature, public_key, operation, openfield)` — **fee and reward are in NO pre-image** (`bismuth_serialize.py:19-20`; `_v2_tx_bytes` `:112-127`; pinned by `tests/test_characterization.py:188,224` where the 8-tuple's reward slot is literally `"0"`). So a fee/reward storage choice can never fork the block hash; its only correctness obligation is **12-field row parity** against the SQLite ledger via `verify_against_sqlite` (`block_store.py:238-255`) and `storage_backend.cross_check` (`storage_backend.py:130-142`). The first revision missed this and assumed `from_units` was the universal target for fee/reward — it is not (see "Numeric-field render mode" below).

This record is the **value half** of `blocks[Codec.hkey(height)]`. The height key (`Codec.hkey`, big-endian u64, C1 / `kvstore.py:74-75`) and the msgpack envelope stay as-is; only each element of `t[]` changes from an 11-field msgpack list to a single `bytes` blob, the `txrec`. The envelope value is msgpack per C6 (`block_store` values are msgpack, never the JSON codec). Today `block_store` stores the row near-verbatim (11-field tuple, `public_key` replaced by an integer id, `block_store.py:90-114`); Stage-4 replaces each per-tx msgpack list with one packed binary `txrec`, recovering the bytes wasted on TEXT amounts and ASCII/hex addresses. **A-hex ban (C0):** every byte below is TRUE bytes in the LMDB value, never hex-in-text.

```
blocks[Codec.hkey(height)] = Codec.pack({          # msgpack envelope (C6)
    "v": 2,                          # NEW: envelope format discriminator (absent/<2 = legacy)
    "h": block_hash,                 # unchanged: hex str, opaque here (block-header domain)
    "a": [addr_field, ...],          # NEW: per-block address dict (raw tag+bytes), storage-only dedup
    "t": [txrec_bytes, ...],         # each a single packed bytes blob (layout below)
})
```

### Address codec — lossless, reversible, with an opaque fallback (FIX for genesis/non-family)

Every *standard* Bismuth address string decodes to a fixed raw layout whose first byte is a non-zero version, so there is no base58 leading-zero ambiguity and decode/encode round-trips exactly. But the ledger also contains **non-address literals** — the genesis row stores `address='genesis'` (`genesis.py:74,93`; regnet `regnet.py:51-54`), and `'genesis'` does **not** decode to any family. The first revision had no fallback; `base58.b58decode('genesis')` yields garbage and would corrupt the reconstructed row (and, since `address` IS in the block-hash pre-image, a non-canonical *recipient* slipping into a v2 block would fork the chain via `block_hash_v2`). The fix: an explicit **opaque-string tag `0xFE`** plus a **write-time round-trip self-check** that routes anything failing family round-trip to `0xFE`.

The canonical wire of one `addr_field` is uniform for all real families and the opaque case, distinct only for the reference case:

```
addr_field (inline) =  u8 addr_tag | u8 addr_len | addr_len bytes        # tags 0x00..0x04, 0xFE
addr_field (ref)    =  u8 0xFF      | u16 LE ref_id                        # reference into "a"[]
```

```
addr_tag  family / case          addr_raw                              reconstructs to (the STORED string)
0x00      RSA (mainnet)          bytes.fromhex(stored)  (28 B)         addr_raw.hex()  -> 56 lowercase hex
0x01      ECDSA  Bis1            base58.b58decode(stored)              base58.b58encode(addr_raw).decode()
0x02      ED25519 Bis1           base58.b58decode(stored)              base58.b58encode(addr_raw).decode()
0x03      Native multisig Bism   base58.b58decode(stored)              base58.b58encode(addr_raw).decode()
0x04      ML-DSA / secp256r1     base58.b58decode(stored)              base58.b58encode(addr_raw).decode()
0xFE      opaque (raw UTF-8)     stored.encode('utf-8')                addr_raw.decode('utf-8')  (verbatim)
0xFF      ref                    --                                    deref a[ref_id], then as above
```

Family routing for the **tag** reuses the consensus dispatcher's regex on the base58 STRING (C5 / `signerfactory.py:120-134`: `^[abcdef0-9]{56}$`→0x00, `^Bis1…{28,52}$`→0x01, longer `Bis1…`→0x02, `^(Bism|mBis)…$`→0x03, versioned-prefix→0x04). **The codec dispatches on the string, stores raw bytes, and reconstructs the string — storage-raw, dispatch-string (C5).** The recovered address handed back is always the exact base58/hex string, so the regex path is never fed raw bytes.

**Encodes the STORED string, not the wire address (FIX).** The fields stored are `str(transaction[1])[:56]` / `[:56]` (`digest_tx.py:50-51`, `digest.py:261-262`). `RE_ECDSA_ADDRESS` permits `Bis1`+{28,52} = up to 56 chars, so the `[:56]` cap can coincide with the full address; the codec **always operates on the already-truncated stored string `s`**, never re-deriving from a wire form. At write time it **asserts the exact round-trip and hard-fails to `0xFE` otherwise**:
- tag 0x00 candidate: require `len(s)==56` and `bytes.fromhex(s).hex()==s`; else → `0xFE`.
- tags 0x01–0x04 candidate: require `base58.b58encode(base58.b58decode(s)).decode()==s`; else → `0xFE`.
- anything else (e.g. `'genesis'`) → `0xFE` directly, content stored verbatim.

The check is total: a string that round-trips goes to its family tag; a string that does not goes to `0xFE` and reconstructs byte-verbatim. There is **no path that silently force-decodes a non-address** — that is the property that makes a reconstructed-address-driven `block_hash_v2` fork impossible.

**Address reference (intra-block dedup, storage-only).** `recipient == address` is common (self-spends, change) and a sender re-sending in one block repeats its address. The per-block dict `"a": [addr_field, ...]` (deduped within the block, each entry the inline `tag|len|bytes` form) lets a tx field be `0xFF | u16 LE ref_id`. This is **storage-only**, identical in spirit to the existing pubkey-by-id (`block_store.py:71-99`): the dict is **per-block, rebuilt from the block itself**, introducing no consensus state and no cross-block ordering dependency (so it does not resurrect the rejected consensus address→key registry, `doc/29 §2`). It is optional: a writer MAY always inline (never emit `0xFF`); the reader handles both. The decoder **hard-fails** (raises, never returns a wrong address) if `ref_id >= len(a)`.

### Integer / numeric field codecs

- `timestamp_cs`: the `'%.2f'` timestamp times 100, as an unsigned **LEB128 varint** (matches `_v2_ts_cs`, `bismuth_serialize.py:101-103`). Current mainnet ts (~1.75e11 cs) is a 6-byte varint; accept up to a u64 source. **Canonical: minimal-length varint; the decoder rejects non-minimal encodings** (else two byte strings reconstruct the same row).
- `amount_units`, `fee_units`, `reward_units`: integer atomic units (C4, `1 BIS = 100_000_000`; `amounts.to_units` `amounts.py:23-25`), each an unsigned **LEB128 varint**. BIS amounts are **bounded** (capped supply) so **u64 width is sufficient** (C4) and a varint over a u64 value is safe. Negative is unrepresentable, matching the `amount >= 0` consensus check (`digest_tx.py:98`). Canonical: minimal-length varint.
- `operation`: `u8 op_len | op_len bytes` UTF-8 (cap `[:30]`, `digest_tx.py:59`). Empty op → single `0x00`.
- `openfield`: `u32 LE of_len | of_len raw bytes` (cap 100000, `digest_tx.py:60`; RingCT ~95 KB needs >u16). Stored as **TRUE raw bytes** in the LMDB value (no hex, no base64), so binary payloads cost their real footprint (avoids the A-hex 2× regression, C0).

#### Numeric-field render mode — the fee/reward zero-form fix (FIX, byte-identity breaker)

The first revision reconstructed `amount`/`fee`/`reward` unconditionally via `amounts.from_units(units)`, which renders `0` as `'0.00000000'`. **That is wrong for the live ledger.** The actual stored strings (`digest.py:276-305`) are:
- coinbase tx: `amount = str(0) = '0'` (`db_amount=0`, `:276,300`); `fee = str(0) = '0'` (`:279,302`); `reward = '{:.8f}'.format(...)` → an `'%.8f'` string (`:278,303`).
- non-coinbase tx: `amount = '%.8f'` string (`:263,300`); `fee = str(fee_calculate(...))` → an `'%.8f'` string since `fee_calculate` returns `quantize_eight(...)` (`essentials.py:299`), e.g. `'0.01000000'`; `reward = str(0) = '0'` (`:282,303`).
- genesis row: `amount='0'`, `fee=0`, `reward=1` (bare ints, `genesis.py:93` / `regnet.py:51`).
- integer-storage mode (`amounts.LEDGER_INTEGER=True`, C0): the same three fields are `str(amounts.to_units(...))`, i.e. a **bare integer-units string** with no decimal point (`digest.py:300-303`).

So the canonical stored form of a money field is one of **three render shapes**: a bare `'0'`/bare-int string, an `'%.8f'` decimal string, or (integer mode) a bare integer-units string. `from_units` reproduces only the middle one. To stay byte-identical to `verify_against_sqlite` (`block_store.py:250` `got == rows`), each of the three numeric fields carries a **2-bit render-mode tag** alongside its varint units; the writer derives the tag by inspecting the *exact source string* (not by guessing a universal target), and reconstruction reproduces that string verbatim:

```
num_field = u8 mode | varint units      # mode in {0,1,2,3}; units = amounts.to_units(stored_string)
  mode 0  BARE      : reconstruct str(units)            # '0', '1', bare integer-units string (int mode + the '0'/'1' literals)
  mode 1  FIXED8    : reconstruct amounts.from_units(units)   # the legacy '%.8f' string, e.g. '0.01000000'
  mode 2  RAWTEXT   : value did NOT round-trip via units; fall to opaque (u32 len | raw UTF-8 bytes), reconstruct verbatim
  mode 3  reserved
```

Writer rule (per field, total and deterministic):
1. Let `s` be the exact stored string. Compute `u = amounts.to_units(s)` inside a guard.
2. If `to_units` raises (non-numeric) → **mode 2 RAWTEXT**, store the bytes verbatim.
3. Else if `from_units(u) == s` → **mode 1 FIXED8**, store `u`.
4. Else if `str(u) == s` → **mode 0 BARE**, store `u`. (Covers `'0'`, `'1'`, and integer-mode bare-units strings.)
5. Else → **mode 2 RAWTEXT** (any other rendering, e.g. an unexpected `'1.0'`), store bytes verbatim.

This makes the writer and `verify_against_sqlite` agree on the **exact** stored bytes for the zero/coinbase/genesis/integer-mode rows instead of assuming `from_units`. It is fully lossless and characterization-locked (see vectors). Note `amount` gets the same `num_field` treatment because coinbase/genesis store `amount='0'` too; this does NOT touch consensus (`_v2_units` re-parses whatever string reconstruction emits and `to_units('0')==to_units('0.00000000')==0`, so `block_hash_v2` is identical either way — only the stored 12-field row must match SQLite, which mode 0/1 now guarantees).

### Full record layout (`txrec`, one element of `t[]`)

```
off  field           type         width   notes
0    timestamp_cs    varint       1..9    minimal LEB128, = round('%.2f' * 100)
..   amount          num_field    2..10   u8 mode | varint units  (mode per writer rule)  OR mode2 opaque
..   fee             num_field    2..10   u8 mode | varint units                          OR mode2 opaque
..   reward          num_field    2..10   u8 mode | varint units                          OR mode2 opaque
..   address         addr_field   var     u8 tag|u8 len|bytes  OR 0xFF|u16 ref            (0xFE opaque fallback)
..   recipient       addr_field   var     u8 tag|u8 len|bytes  OR 0xFF|u16 ref            (0xFE opaque fallback)
..   <signature>     (unchanged)  var     existing slot, OUT OF SCOPE (signers domain)
..   <public_key id> (unchanged)  var     existing pk-dedup id, OUT OF SCOPE (block_store.py:95-97)
..   operation       lp-u8        1+var   u8 len + UTF-8
..   openfield       lp-u32       4+var   u32 LE byte-len + raw bytes
```

Field order groups the leading scalars (`timestamp_cs` + the three `num_field`s) first so a reader can skip them in one pass, then the two addresses, then the two unchanged crypto slots (kept exactly as today so the signers domain is independent), then op/openfield last.

### Reconstruction rule (C-RECON: lossless round-trip, proof)

On `get_block` (`block_store.py:133-139`) / `_expand` (`:90-99`), the v2 branch decodes each `txrec` and rebuilds the **exact 12-field ledger row** in canonical column order:

1. `block_height` = the key via `Codec.unhkey` (int — matches the SQLite `r[0]` int that `_grouped_blocks` yields, `block_store.py:208-213`).
2. `timestamp` = `"{}.{:02d}".format(cs // 100, cs % 100)` — exact integer formatting, reproduces the `'%.2f'` string (`digest_tx.py:49`) with no float.
3. `address` / `recipient` = resolve `addr_field` (deref `0xFF` into `a[]`, bounds-checked) then re-encode by tag exactly as the table above (0x00 → `.hex()`; 0x01–0x04 → `base58.b58encode(...).decode()`; 0xFE → `.decode('utf-8')`).
4. `amount` = render `num_field[amount]` per its mode (mode 0 → `str(units)`; mode 1 → `from_units(units)`; mode 2 → verbatim bytes decoded).
5. `signature`, `public_key` = unchanged slots; pk id re-expanded as today (`block_store.py:95-97`).
6. `block_hash` = `"h"` (unchanged).
7. `fee` = render `num_field[fee]`; `reward` = render `num_field[reward]` — both per the same mode logic, so `'0'` stays `'0'` and `'0.01000000'` stays `'0.01000000'`.
8. `operation` = `op_bytes.decode('utf-8')`; `openfield` = `of_bytes.decode('utf-8')` when it originated as a str (the common case, `digest_tx.py:60` stores `str(...)[:100000]`); a binary openfield is returned as raw bytes (the existing code tolerates `bytes` via `isinstance` guards, `bismuth_serialize.py:86,119`).

**Round-trip proof.** Each step is the exact inverse of the canonical→storage map: every field is either (a) an integer with a fixed decimal scale whose original *rendering* is captured by the 2-bit mode, (b) a varint timestamp on a fixed centisecond scale, (c) an address with a non-ambiguous decode plus a verbatim opaque fallback, or (d) length-prefixed raw bytes. Therefore the rebuilt 12-field tuple is **byte-identical** to the SQLite row, which is precisely what `verify_against_sqlite` (`block_store.py:250`) and `storage_backend.cross_check` (`storage_backend.py:130-142`) assert — they compare reconstructed rows, not stored bytes, and continue to pass unchanged.

### Fee-weight straddling-window dispatch (FIX, consensus)

`recent_block_weights` (`block_store.py:141-157`) feeds `node.base_fee` via `fee_dynamics.base_fee` (`digest.py:582-593`), which is a **consensus** value at/after the fork. The first revision globally swapped the openfield weight from `len(str(r[-1]))` (legacy char count) to the decoded `of_len` (byte count). Across a fee window that **straddles** the fork, that retroactively changes the weight — hence `base_fee` — of the historical legacy-envelope blocks read in the same window. The fix is **per-block dispatch by envelope version**, never a blanket switch:

```
# recent_block_weights, per block in the window:
env = _unpack(v)                       # {"v":2,"a":..,"t":[..]}  OR legacy {"h","t":[..]}
if env.get("v", 0) >= 2:               # v2 envelope: txrec blobs -> decode of_len (BYTE count)
    ofbytes = sum(txrec_openfield_len(blob) for blob in env["t"])   # cheap: read the trailing u32 LE of_len
else:                                   # legacy envelope: 11-field lists -> keep char-count semantics
    ofbytes = sum(len(str(r[-1])) for r in env["t"])
weights.append(len(env["t"]) + ofbytes // unit)
```

A legacy block in the window keeps `len(str(r[-1]))` forever; a v2 block uses the decoded `of_len`. This keeps the post-fork `base_fee` derived from any straddling window deterministic and identical to the SQLite-era result for the legacy portion. (For the common ASCII openfield the two counts coincide; they differ only for multi-byte/binary openfields, which post-fork v2 blocks measure by true byte count — the intended congestion signal.)

### Fork gate (by destination height) and pre-fork byte-identity

The encoding selector is **one signal only**: `node.fork_height` (C0). The gate is evaluated **per block, by the destination height**, at the canonical block-write seam. The current write path is `digest.py:734-737` → `node.block_writer.append_block(height, block_hash, rows)` → `LmdbWriteBackend.append_block` → `block_store.put_block` (`storage_backend.py:200-203`). The gate threads the destination height and `node.fork_height` to `put_block`/`append_block`:

```
# at the write seam (digest.py:736), pass through to put_block:
post_fork = (node.fork_height is not None
             and block_instance.block_height_new >= node.fork_height)
```
- `post_fork is True`  → write the Stage-4 `txrec` form in a `{"v":2,"h":..,"a":..,"t":[..]}` envelope.
- `post_fork is False` (**includes `fork_height is None`, i.e. 100% of current mainnet by construction**, C0) → write the existing `{"h","t":[11-field msgpack lists]}` envelope unchanged (`block_store.py:114`).

The reader dispatches on `env.get("v", 0)`: `<2` (absent or 0/1) → legacy `_expand` (`block_store.py:90-99`); `==2` → the Stage-4 decoder above. Gating is **by the height of the block the bytes belong to**, never a global mode: a historical block written in the legacy envelope is read back with the legacy decoder forever, so re-serialization is height-stable and byte-identical regardless of node config. `amounts.LEDGER_INTEGER` is a **storage flag, decoupled from the gate** (C0): the gate never reads it; the varint codec consumes integer units via `to_units`, and the `num_field` mode (BARE vs FIXED8) is what reproduces the integer-mode bare-units string vs the legacy decimal string — so reconstruction is correct under either storage mode without consulting the flag in the gate.

Pre-fork stays byte-identical three ways: (1) the frozen pre-image functions (`bismuth_serialize.py:23-55,77-147`) are untouched; (2) no v2 record is ever written below `fork_height`, so the legacy on-disk region is never rewritten (C0 — the 23 GB prod ledger is never force-rebuilt); (3) reconstruction reproduces the exact `'%.2f'`/`'%.8f'`/`'0'`/base58/56-hex strings, so any signature buffer or block hash computed from a Stage-4-stored row equals the one from a SQLite row.

### Code that changes
- `block_store.py:90-99` (`_expand`): add the `env.get("v",0)>=2` branch decoding `txrec` + the `"a"` dict; bounds-check `ref_id`.
- `block_store.py:102-118` (`put_blocks`/`put_block`): accept the `post_fork` flag (or destination height + `fork_height`); when post-fork, encode each `txrec` (with the `num_field` writer rule and the address round-trip self-check) and build the per-block `"a"` dict; legacy path unchanged.
- `block_store.py:141-157` (`recent_block_weights`): per-block openfield-weight dispatch by envelope `"v"` (above).
- `storage_backend.py:180-203` (`StorageWriteBackend.append_block`/`LmdbWriteBackend.append_block`): thread `post_fork` (or height+`fork_height`) into `put_block`.
- `digest.py:736-737`: pass `post_fork` (computed from `node.fork_height` and `block_instance.block_height_new`) to `append_block`.
- New module `txrec.py` (encode/decode + address codec + `num_field` codec), dependency-light beside `bismuth_serialize.py`; characterization-locked like `tests/test_characterization.py:177,185`.
- **No changes** to `bismuth_serialize.py`, `amounts.py`, or any frozen function.

### Characterization vectors (must exist BEFORE the canonical writer emits this format)

Add to `tests/test_characterization.py` and lock:
- Per-family address round-trip: RSA `0x00` (56-hex), ECDSA `0x01`, ED25519 `0x02` (including a 56-char `[:56]`-boundary Bis1), multisig `0x03` (`Bism…`), ML-DSA/secp256r1 `0x04`, opaque `0xFE` (`'genesis'` and an arbitrary non-address recipient), and the `0xFF` ref form (plus an out-of-range `ref_id` that MUST raise).
- Numeric-field modes: a non-coinbase row (`amount` FIXED8, `fee` FIXED8 e.g. `'0.01000000'`, `reward` BARE `'0'`); a coinbase row (`amount` BARE `'0'`, `fee` BARE `'0'`, `reward` FIXED8); the genesis row (`amount='0'`, `fee=0`→`'0'`, `reward=1`→`'1'`); and an integer-mode row (`amounts.LEDGER_INTEGER=True`, all three BARE bare-units strings).
- Non-minimal-varint rejection; full-`txrec` round-trip equals the input 12-field tuple byte-for-byte.

Gate to primary (C7): only after `verify_against_sqlite`/`storage_backend.cross_check` are byte-for-byte on the reconstructed forms AND `replay_verify` reports **0 mismatches** at `fork_height=None` and across a straddling (pre+post-fork) regnet chain.


**Savings**

| field | legacy form | legacy bytes | true-bytes form | new bytes | saving % |
|---|---|---|---|---|---|
| timestamp | `'%.2f'` TEXT (e.g. `1750000000.00`) | 13 | minimal varint centiseconds (~1.75e11 cs) | 6 | 54% |
| amount | `'%.8f'` TEXT (e.g. `1.00000000`) | 10 | `num_field` = u8 mode + varint units (1 BIS=1e8→4B) | 5 | 50% |
| fee | `'%.8f'` TEXT (e.g. `0.01000000`) | 10 | `num_field` = u8 mode + varint units (1e6→3B) | 4 | 60% |
| reward | bare `'0'` TEXT (non-coinbase) | 1 | `num_field` = u8 mode 0 + varint 0 (1B) | 2 | -100% (1B overhead on zero; dwarfed by other wins) |
| reward (coinbase) | `'%.8f'` TEXT (e.g. `5.00000000`) | 10 | `num_field` = u8 mode 1 + varint units (~few e9→5B) | 6 | 40% |
| address | 56-hex (RSA) / ~34-char base58 TEXT | 56 / 34 | u8 tag + u8 len + 28 raw (RSA) / ~25 raw (Bis1) | 30 / 27 | 46% / 21% |
| recipient | 56-hex / ~34-char base58 TEXT | 56 / 34 | u8 tag+u8 len+raw, or `0xFF`+u16 ref (self-spend) | 30 / 27, or 3 if ref | 46% / 21% (91% ref) |
| operation | TEXT (empty on plain send) | 0 | u8 len + UTF-8 | 1 | -∞ (1B overhead when empty) |
| openfield | TEXT (raw, or hex/base64 binary payload) | n / 2n | u32 len + TRUE raw bytes | 4+n | ~0% on raw text; ~50% on binary (no hex/b64 2×) |
| **row total (RSA, plain send, empty op/of, non-coinbase)** | **sum non-sig/non-pk fields** | **~146** | **packed txrec** | **~78** | **~47%** |


**Adversarial fixes folded in:**
- fee/reward (and amount) zero/coinbase/integer-mode byte-identity: REPLACED the blanket from_units reconstruction with a 2-bit render-mode num_field (u8 mode + varint units). The writer derives the mode from the EXACT source string (mode 0 BARE for str(units) e.g. '0'/'1'/integer-mode bare-units; mode 1 FIXED8 for the '%.8f' string; mode 2 RAWTEXT verbatim fallback), proven against the real write sites digest.py:276-305 (coinbase amount='0',fee='0',reward='%.8f'; non-coinbase amount='%.8f',fee='%.8f',reward='0') and genesis.py:93/regnet.py:51 (amount='0',fee=0,reward=1). Reconstruction reproduces the precise stored string so verify_against_sqlite (block_store.py:250 got==rows) stays byte-for-byte. Characterization-locked for coinbase, non-coinbase, genesis, and integer-mode rows.
- opaque/non-family address fallback: ADDED tag 0xFE (raw UTF-8, u8 len + bytes) plus a TOTAL write-time round-trip self-check (fromhex(s).hex()==s for 0x00; b58encode(b58decode(s)).decode()==s for 0x01-0x04) that routes any string failing family round-trip — including 'genesis' (genesis.py:74,93 / regnet.py:51-54) and any anomalous recipient — to 0xFE, stored and reconstructed verbatim. No path force-decodes a non-address, so a reconstructed-address-driven block_hash_v2 fork is impossible. Characterization-locked for 'genesis' and an arbitrary non-address recipient.
- straddling-window fee weight: CHANGED recent_block_weights (block_store.py:141-157) to dispatch the openfield weight PER BLOCK by envelope version — legacy envelope (v absent/<2) keeps len(str(r[-1])) char-count, v2 envelope uses the decoded of_len byte-count read from the txrec's trailing u32 LE — instead of a global switch. This keeps post-fork base_fee (digest.py:582-593, a consensus value) derived from a straddling window deterministic and identical to the SQLite-era result for the legacy portion.
- encode the STORED string, not the wire address: STATED explicitly that the codec operates on the already-[:56]-truncated stored string (digest_tx.py:50-51 / digest.py:261-262), asserts b58encode(b58decode(stored))==stored (and fromhex(stored).hex()==stored for RSA) at write time, and hard-fails to the 0xFE opaque tag on any string that violates round-trip rather than corrupting the row. Covers the ED25519 56-char [:56]-boundary case.
- characterization vectors: SPECIFIED the required tests in tests/test_characterization.py — per-family address vectors (0x00 RSA, 0x01 ECDSA, 0x02 ED25519 incl. 56-char boundary, 0x03 multisig, 0x04 ML-DSA/secp256r1, 0xFE opaque, 0xFF ref incl. out-of-range raise), the zero/coinbase/genesis/integer-mode num_field rows, non-minimal-varint rejection, and a straddling fee window with both envelope kinds; and required replay_verify 0-mismatch on a straddling chain AND verify_against_sqlite/storage_backend.cross_check byte-for-byte BEFORE the canonical writer emits this format (C7 per-projection flip gate).


**Implementation checklist (residual review items to satisfy before wiring this domain):**
- [ ] Fix the num_field writer rule to be storage-MODE aware. The stored money string s has TWO possible numeric meanings: decimal BIS ('%.8f' or '0') OR bare atomic units (when amounts.LEDGER_INTEGER is True). The rule must derive units correctly for each: in integer-storage mode units = int(s) (the string IS units) reconstructed via str(units) for the bare form; in decimal mode units = amounts.to_units(s) reconstructed via from_units(units). Concretely, replace the single 'u = to_units(s)' with: try int(s) for a bare-integer-units candidate (mode BARE, reconstruct str(units)); else try to_units(s) with from_units round-trip (mode FIXED8); else mode RAWTEXT. Note 'BARE' must reconstruct str of the SAME integer that was decomposed (no 1e8 round-trip), or it can never match a bare-units row.
- [ ] Re-baseline the characterization vectors to the ACTUAL writer output. Specifically: a decimal-mode coinbase reward '%.8f' -> FIXED8; a decimal-mode bare '0' -> BARE; an INTEGER-mode amount/fee/reward bare-units string -> BARE via the int(s) path (NOT to_units); and a bare-int reward like '1' must be classified correctly by whatever path you choose. The current vectors assert BARE for '1' and integer-mode rows that the current rule actually emits as RAWTEXT -- the test as written would fail or, worse, lock in the broken RAWTEXT classification and silently kill compaction.
- [ ] Remove or correct the genesis num_field characterization vector: genesis (height 1) is pre-fork and uses the legacy msgpack envelope; its fee/reward/operation are stored and read back as Python INTS (0,1,1), not strings. Either drop genesis from the txrec/num_field vectors (it never exercises that path) or, if you keep it as a guard, assert it goes through the LEGACY envelope and round-trips byte-identically with int types preserved.
- [ ] Correct the savings table and risk text: amount/fee/reward compaction is realized ONLY in decimal-storage mode under the current/fixed rule; document the integer-storage-mode varint path explicitly once the mode-aware rule lands, or the table's '5 bytes' amount figure is unachievable whenever LEDGER_INTEGER=True.

---

## 4. Block header / hashes / txid list  `[block-header]`

## hf2 Stage-4 TRUE-BYTES storage: Block header / hashes / txid list

> Obeys the SHARED CROSS-DOMAIN CONVENTIONS verbatim. Where this section names a convention (C0–C8) it is deferring to that shared contract; where it pins bytes it does so consistently with it. **This is a v2-region-only change**: the legacy on-disk region is frozen byte-for-byte (C0 "Legacy on-disk byte-identity").

### 0. Scope and the inefficiency being closed

This domain covers the block-level identity fields stored in `block_store.py`'s LMDB env:

- **`block_hash`** (the block's own hash) — today carried as a **hex string** in the msgpack value `{"h": block_hash, "t": [...]}` (`block_store.py:114`), and **again per-tx** at stored-row index 6 (the 11-field stored row — `block_store.py:48-50` documents the order `timestamp,address,recipient,amount,signature,public_key,block_hash,fee,reward,operation,openfield`; only `block_height` is dropped via `r[1:]` at `block_store.py:111`, so `block_hash` is **retained** in every tx today), and **a third time** as the raw key of the `hashes` sub-db (`block_store.py:115`, key = `self._bh(block_hash) = block_hash.encode()` — i.e. the **hex string encoded as UTF-8 bytes**, an A-hex regression per C0).
- **`previous_hash`** — never stored explicitly; it is `blocks[height-1]`'s block hash, and is the consensus input fed at `digest.py:625`. No change: handled entirely by reconstruction (§3).
- the per-block **txid list** — not stored today; post-fork it exists only as the global `txid_index` (`txid -> height`, raw 8B BE value, `txid_index.py:77`). There is no forward `height -> [txids]` list.
- block **height** — the key, already TRUE bytes: `Codec.hkey = struct.pack(">Q", h)` = 8-byte big-endian uint64 (per **C1**). **No change.**
- merkle root — Bismuth has **none**. The block hash is `sha224(repr(8-tuple-list)+prev)` (legacy `block_hash`, `bismuth_serialize.py:50-55`) / `blake2b-256(per-tx-binary + prev)` (`block_hash_v2`, `bismuth_serialize.py:130-147`). No separate merkle field exists to store. The §2.3 txid list serves the "what's in this block" role.

**The waste (v2 region only):** a hex hash is 2× its raw bytes (the A-hex regression, C0). A v2 64-hex blake2b string is **64 bytes**; its digest is **32 raw bytes** (`digest_size=32` per **C2**). Stored 2–3× per block, plus a hex-encoded `hashes` key.

### 1. The fork gate (by destination height) and the strict legacy/v2 split

**The encoding is selected per block by the DESTINATION block height against the single signal `node.fork_height`** (C0), mirroring the consensus gate `block_hash_at(height, fork_height, ...)` (`bismuth_serialize.py:151-156`) and its call site `digest.py:624-626`. Never a global mode; never `amounts.LEDGER_INTEGER` (a decoupled storage flag, C0).

**STRICT split (FIX C — the legacy region is NOT rewritten):**

- `fork_height is None` (mainnet today — 100% of blocks) **OR** `height < fork_height` ⇒ **LEGACY region**: the block keeps its **EXISTING on-disk encoding byte-for-byte** — the msgpack value `{"h": <hex str>, "t": [...]}` with the per-tx hash retained at stored index 6, and the `hashes` key as `block_hash.encode()` (hex-as-UTF8). **No re-encode, no new sub-db writes.** This preserves on-disk byte-identity for every current mainnet block (all legacy today), keeps `test_lmdb_on_disk_bytes_identical_to_direct_lmdb` (`tests/test_kvstore.py:114`, `tests/test_balance_index.py:96`) and `verify_against_sqlite` (`tests/test_block_store.py:189`) green on the legacy region, and never force-rebuilds the 23 GB prod ledger (C0, C7).
- `height >= fork_height` ⇒ **v2 region**: the new true-bytes binary record (§2). The block hash is stored once as **raw 32 bytes**, dropped from every per-tx row, and the `hashes` key/`txids` blob are raw bytes.

Because the gate is the block's **own** height, a resync with `fork_height=None` writes 100% legacy by construction; a straddling chain writes each block on the branch its height dictates — both reproduce byte-identical consensus inputs via §3. This is a **storage** decision behind the frozen serialization boundary; the consensus pre-images (`block_hash`, `block_hash_v2`, `block_hash_at`, `tx_id*`, `signature_buffer*`, `signed_message`) are **NOT mutated** (C0).

A reader distinguishes the two encodings without a stored discriminator: the **value's first byte** is the discriminant. A legacy value is msgpack (`Codec.pack({"h":...})`) whose first byte is a msgpack map marker (`0x80–0x8f` fixmap, or `0xde/0xdf`). The v2 record begins with the **format-version byte `0x04`** (the §2 record's leading byte, aligned with the C8 `meta[b"fmt"]=b"\x04"` env sentinel). `0x04` cannot collide with any msgpack map first-byte, so the dispatch is unambiguous; it is also redundantly confirmed by the height gate. **The env-level C8 sentinel governs which encoding the writer emits and refuses to serve consensus reads from a stale-format env.**

### 2. On-disk byte layouts (v2 region only)

Multi-byte integers are little-endian **except height keys**, which stay big-endian (`Codec.hkey`, C1). "TRUE bytes" = raw bytes in an LMDB key/value, never hex-in-text (C0 A-hex ban). All blake2b widths are pinned per **C2**: block hash and txid are both `digest_size=32`.

#### 2.1 `blocks` sub-db value — v2 binary block record

Replaces (for v2-region heights only) today's msgpack `{"h": <hex>, "t":[...]}`. The block hash is stored **once** as raw 32 bytes and **removed from every per-tx row** (a block-level constant, not a tx field).

```
field        | type                      | width   | notes
-------------+---------------------------+---------+-----------------------------------------
fmt          | u8 = 0x04                 | 1       | v2 record marker (== C8 env sentinel); also discriminates vs msgpack legacy value
block_hash   | raw blake2b-256 digest    | 32      | bytes.fromhex(block_hash); the ONE block-level hash
ntx          | u32 LE                    | 4       | per-tx record count (all rows incl. negative-height mirrors)
tx_blob[ntx] | per-tx binary records      | var     | each row WITHOUT the block_hash column (see §2.4)
```

`fmt` is fixed `0x04` (not a length discriminator — hash width is always 32 in the v2 region, since v2 hashes are blake2b-256). It makes the value standalone-decodable and lines up with the C8 env sentinel.

#### 2.2 `hashes` sub-db (reverse: hash -> height) — raw-bytes key (v2 region)

```
key   | type                   | width | notes
------+------------------------+-------+--------------------------------
hash  | raw blake2b-256 digest | 32    | bytes.fromhex(block_hash), NOT block_hash.encode()
val   | u64 BE (Codec.hkey)    | 8     | height (C1; unchanged)
```

For v2-region blocks the key is `bytes.fromhex(block_hash)` (32 raw bytes), replacing today's hex-as-UTF8 `block_hash.encode()`. 32-byte keys are far under LMDB's 511-byte cap. **Legacy-region `hashes` entries keep `block_hash.encode()` unchanged.** A reverse lookup against this db must therefore try both forms across the boundary, or — cleaner — gate by height: a v2-region query hex-decodes first, a legacy query encodes (see §6). The value is always 8-byte BE height (C1), shared with `block_store.blocks` height keys per C1.

#### 2.3 `txids` sub-db (NEW: forward height -> raw txid list) — v2 region only

A per-block forward list of the **raw 32-byte content txids** (C3: post-fork there is exactly ONE txid byte form, `tx_id_v2`, stored raw-32), in canonical tx order. The merkle-leaf-set analogue; backs fast block-contents / proof queries without re-hashing rows.

```
key   | type          | width                      | notes
------+---------------+----------------------------+----------------------------------
hkey  | u64 BE        | 8                          | block height (C1)
val   | raw txid blob | 32 * n_postfork            | fixed 32B stride, no length prefix
```

`val` layout: `txid_0(32) || txid_1(32) || ... || txid_{n-1}(32)`; the i-th txid is `val[32*i : 32*i+32]`, `n = len(val)//32`.

**FIX (filter parity):** `n` MUST equal the **post-fork positive-height tx count**, computed with the **IDENTICAL filter** as `txid_index.apply_rows` (`txid_index.py:75`: `if h <= 0 or h < int(fork_height): continue`) — i.e. **skip negative-height reward-mirror rows AND any row below `fork_height`**, and store `bytes.fromhex(txid_of(r, fork_height))` for the rest (`txid_index.txid_of`, `txid_index.py:45-50`). The blob is therefore the **inverse** of the global `txid_index` (`txid -> height`); the global index is unchanged and complementary. This is fed from `processor.block_transactions` — the same source `txid_index.apply_rows` consumes at `digest.py:834` — so the two stay in lockstep by construction. An invariant test (§5) asserts `len(txids[height])//32 == len([r for r in block_transactions if r[0] > 0 and r[0] >= fork_height])` for straddling and post-fork blocks.

#### 2.4 Per-tx record inside `tx_blob` (block_hash column dropped)

Within a v2 block record the per-tx encoding is the existing 11-field stored row **minus its `block_hash` column** (stored index 6), in binary. The remaining field encodings (ts/amount integer vs string, sig/pubkey raw-vs-base64, pubkey-by-reference) are owned by the **signers** and **consensus-serial** domains — this section asserts only the structural fact that **the `block_hash` column is removed and re-synthesised on read**, and that the `pubkey_id` dedup indirection is retained unchanged.

```
field        | owned by              | notes
-------------+-----------------------+-----------------------------------------------
ts           | consensus-serial      |
addr/recip   | consensus-serial      |
amount       | amounts (C4: BIS u64) |
signature    | signers               |
pubkey_id    | THIS domain (u64 LE)  | UNCHANGED: pk/pkr dedup, block_store.py:50,71-88; blake2b-32 key (C2), BE-u64 id, raw RSA value
[block_hash] | THIS domain           | *** DROPPED *** (block-level constant; re-synthesised on read at expanded index 7, §3)
fee/reward   | amounts (C4: BIS u64) |
operation    | consensus-serial      |
openfield    | consensus-serial      |
```

The `pubkey_id` indirection (`pk`/`pkr` sub-dbs, `block_store.py:59-88`) is **unchanged** and already TRUE-bytes per **C2/C6** (`pk` key = `blake2b(pubkey, digest_size=32)`; `pkr` value = raw pubkey, no codec). The **only** block-header-domain change to the per-tx record is removing the redundant `block_hash` column (saving 32–64 bytes × ntx per block).

### 3. Reconstruction rule (C-RECON; lossless, reversible) — precedence pinned

To rebuild the exact wire/consensus hex string from stored raw bytes:

```
reconstruct_block_hash(raw_bytes) -> str:
    # SOLE authoritative length source: len(raw_bytes).
    #   32 raw bytes -> 64 lowercase hex (v2 blake2b-256)
    #   28 raw bytes -> 56 lowercase hex (legacy sha224, never produced by the v2 record;
    #                   only relevant if a legacy parent's digest is ever handed in raw)
    return raw_bytes.hex()
```

**FIX B — precedence pinned explicitly.** `len(raw_bytes)` is the **SOLE authoritative source** for the reconstructed hex length and bytes. The `fmt=0x04` marker and the `height >= fork_height` gate are **advisory consistency checks only**: they MUST NEVER reject a block or alter the reconstructed bytes. If `fmt`/the height-gate ever disagree with `len(raw_bytes)`, the reconstruction still returns `raw_bytes.hex()` and the disagreement is logged (not raised) on the read path. This avoids the validate/apply desync class (2026-06 audit): a `fork_height` misconfiguration on read can never reject a valid block, because reconstruction is fork_height-independent.

**Proof of losslessness.** Both emitters use `.hexdigest()` (lowercase): legacy `bismuth_serialize.py:55`, v2 `bismuth_serialize.py:147`. So `bytes.fromhex(h).hex() == h` exactly (no case/normalisation loss) for any block hash this store holds.

**Per-tx `block_hash` re-synthesis — exact index (FIX, cross_check correctness).** On read, `_expand` (`block_store.py:90-99`) re-inserts the reconstructed hex block_hash into each row at **expanded 12-field index 7** — i.e. **stored 11-field index 6 after the `[height]` prepend**. Concretely, alongside the existing `t = [height] + t` prepend and pubkey re-expansion, the v2 path does `t.insert(7, reconstruct_block_hash(block_hash_raw))`. Index 7 is mandatory and verified two ways: the 12-field SQLite column order has `block_hash` at index 7 (`_grouped_blocks` yields `group[0][7]` as the block hash, `block_store.py:211,215`), and `storage_backend.cross_check` asserts `candidate.block_hash(height) == rows[0][7]` (`storage_backend.py:139`). So `get_block(height)` still returns exact 12-field SQLite-identical rows.

**`block_store.block_hash(height)` (`block_store.py:159-162`)** returns `reconstruct_block_hash(block_hash_raw)` for a v2 record (raw 32B → 64-hex), and the existing `_unpack(v)["h"]` for a legacy record — i.e. the hex string in both cases, so `storage_backend.py:139` and the linkage trust-check at `digest.py:752` keep working.

**`previous_hash` reconstruction.** `previous_hash` is not stored; it is `reconstruct_block_hash(blocks[height-1].block_hash_raw)` for a v2 parent, or `blocks[height-1]["h"]` for a legacy parent. Either way it yields the exact 56-/64-hex string consensus expects at `digest.py:625` / `block_hash_v2`'s prev handling.

**`txids` round-trip.** `val[32*i:32*i+32].hex()` reproduces the 64-hex `tx_id_v2` string (`txid_index.txid_of`, `txid_index.py:45`); storing `bytes.fromhex(txid_of(...))` is lossless for the same lowercase-hex reason.

### 4. The boundary case (a v2 block with a legacy 56-hex sha224 parent)

`previous_hash` is reconstruction-only (§3), so the boundary needs **no special stored form**:

- Block at `height = fork_height` (first v2 block): its **own** hash is the v2 binary record's raw 32 bytes → reconstructs to 64-hex. Its **parent** `blocks[fork_height-1]` is a **legacy record** (kept in its existing msgpack/hex form per FIX C), whose `"h"` is already the 56-hex sha224 string. Feeding that 56-hex string into `block_hash_v2(..., previous_hash=<56-hex>)` hits the documented one-time UTF-8 branch (`bismuth_serialize.py:140-146`: `len(prev) != 64` ⇒ `prev.encode("utf-8")`). No special case in the store.
- Genesis / height 0: below any `fork_height`, legacy branch, untouched.

So the boundary reduces to: *a v2 block can have a legacy parent*; the strict legacy/v2 split handles it natively because the parent is still its exact legacy bytes and `block_hash_v2` already knows the 64-vs-56 rule.

### 5. Pre-fork byte-identity and replay validation (C0 "Replay-validated")

- **Frozen consensus functions untouched** — `block_hash`, `block_hash_v2`, `tx_id`, `tx_id_v2`, `tx_id_at`, `block_hash_at`, `signature_buffer*`, `signed_message` keep their exact current bytes (`bismuth_serialize.py`). This is a storage-representation change behind the serialization boundary, like the existing pubkey-id dedup (`block_store.py:59-88`) already proven byte-transparent.
- **`fork_height=None` (mainnet today):** every block is legacy ⇒ its on-disk bytes are **literally unchanged** ⇒ `test_lmdb_on_disk_bytes_identical_to_direct_lmdb` stays green and `replay_verify` reports **0 mismatches** trivially.
- **Straddling chain (fork_height set):** legacy-region blocks keep their exact bytes; v2-region blocks store the §2 binary form and `get_block` reconstructs identical 12-field rows. `replay_verify` re-derives identical block hashes ⇒ **0 mismatches** across the boundary block (legacy 28-byte-digest/56-hex parent feeding a v2 32-byte-digest/64-hex child).
- **`storage_backend.cross_check(SqliteBackend, LmdbBackend)`** stays byte-for-byte: `get_block(h)` rows equal SQLite rows (reconstructed hex block_hash at index 7), and `LmdbBackend.block_hash(h) == rows[0][7]` (`storage_backend.py:139`) holds because `block_store.block_hash` returns the reconstructed hex, not raw bytes. Cross-check must be run **across the boundary block** explicitly.
- **New invariant tests (FIX):** (a) `len(txids[height])//32 == post-fork positive-height tx count` for straddling and post-fork blocks; (b) a reorg test asserting no stale `hashes`/`txids` survive a rollback across the fork boundary (§6); (c) re-baselined characterization of the **v2-region** binary form (the legacy-region characterization is unchanged).

### 6. Dispatch / gate sites that change

- **Write — `block_store.put_blocks` (`block_store.py:102-115`).** Gate each `(height, block_hash, rows)` by `height >= fork_height` (C0):
  - **Legacy-region height (or `fork_height is None`):** unchanged — write `_pack({"h": block_hash, "t": txs})` to `blocks` and `self._bh(block_hash)` to `hashes` exactly as today (preserves on-disk identity, FIX C).
  - **v2-region height:** write the §2.1 binary record (leading `0x04`, raw 32-byte `bytes.fromhex(block_hash)`, `ntx` u32 LE, per-tx records with the block_hash column dropped from each `t`); write the `hashes` key as `bytes.fromhex(block_hash)` (raw 32B) → 8B BE height; write the §2.3 `txids` blob.
- **Write (new sub-db).** Add `"txids"` to the `dbs=[...]` list at `block_store.py:55`; populate it in `put_blocks` for v2-region heights using the C3/filter-parity rule (§2.3), fed from the same `processor.block_transactions` source as `txid_index.apply_rows` (`digest.py:834`).
- **Read — `block_store._expand` (`block_store.py:90-99`).** For a v2 record, alongside the existing `[height]` prepend and pubkey re-expansion, `t.insert(7, reconstruct_block_hash(block_hash_raw))` (FIX: exact index 7). For a legacy record, the path is unchanged (the per-tx hash is still present at stored index 6 → expanded index 7). `block_store.block_hash` (`block_store.py:159-162`) returns the reconstructed hex (v2) or `_unpack(v)["h"]` (legacy). `block_store.height_by_hash` (`block_store.py:164-167`) hex-decodes its argument before probing the v2 `hashes` key, falling back to / disambiguating the legacy `block_hash.encode()` form by height (legacy keys coexist for the legacy region in the same env).
- **Raw-consumer audit (risk).** `recent_block_weights` (`block_store.py:141-157`) reads the raw record's `["t"]` and indexes `r[-1]` for openfield length. For the v2 binary record this raw shape changes (no msgpack `["t"]`, openfield is the last length-prefixed field of each §2.4 tx record); the v2 branch of `recent_block_weights` must decode the binary record's per-tx openfield, NOT assume the msgpack list. Every raw consumer of the `blocks` value must branch on the `0x04`/msgpack discriminant or go through a decode helper — never assume one shape.
- **Rollback — `block_store.rollback` (`block_store.py:120-130`) (FIX).** Currently derives the `hashes` delete-key via `self._bh(_unpack(v)["h"])`, which assumes msgpack-with-`"h"`. For a v2 record this breaks. The fix: when the scanned value begins with `0x04`, parse the raw 32-byte block hash out of the binary record (bytes `[1:33]`) for the `hashes` delete-key; otherwise use `self._bh(_unpack(v)["h"])` for legacy. In the **same write txn**, also `txn.delete(self.txids, k)` for the height. A reorg test asserts no stale `hashes`/`txids` survive a rollback across the fork boundary.
- **Consensus block-existence read — `block_already_exists` (`digest.py:874-888`) (FIX, atomicity).** Today it queries SQLite `transactions WHERE block_hash = ?` with the **hex** string. When this read migrates onto `block_store.height_by_hash`, the v2 `hashes` key encoding flips from hex-as-UTF8 (`block_store.py:68-69`) to `bytes.fromhex` raw digest. Old and new key encodings **cannot coexist for the same block** in one env, so the v2-region read migration and the v2-region rebuild MUST be atomic (governed by the C8 env sentinel and the per-projection migration flip, C7): `build_from_sqlite` runs from a **snapshot copy**, never a hot full-scan of the live 23 GB ledger (C0, C7). The legacy region's `hashes` reads keep the hex-as-UTF8 key; the migration flip never re-keys legacy entries.
- **Consensus gate (unchanged, already correct).** `digest.py:624-626` (`block_hash_at` by `block_height_new`) and `bismuth_serialize.py:151-156` remain the single height-driven gate; `node.fork_height` is the only signal (C0).

### 7. Per-env, snapshot, and migration discipline (C7/C8)

`block_store` is the **canonical** store and lives in **its own append-only env** with 100 GB+ `map_size` headroom (C7); it must NOT share an env with any projection — in particular `reward_chain`'s negated keyspace can never alias it (C1 HARD INVARIANT: `block_store` is positive-real-height-only, no negative-height reward-mirror rows). `txids` is a sub-db **within the block_store env** (it is part of the canonical block body, derived deterministically from the stored rows; it is not an independently-rebuildable projection of a different store). The env carries the C8 sentinel `meta[b"fmt"]=b"\x04"`: an operator-copied pre-Stage-4 env (hex-keyed) opened under new code is detected by the absent/lower sentinel and **refuses to serve v2-region consensus reads until rebuilt** (canonical store policy, C8), rather than silently missing raw-byte key probes. The v2-region rebuild (`build_from_sqlite` from a snapshot) writes the sentinel atomically with its first commit. Snapshots are the compacted env files themselves (`mdb_copy --compact`) plus the C7 manifest `{tip_height, tip_hash, fork_height, per-env state-root}` — never a re-serialized dump (that would void byte-identity).


**Savings**

| field | legacy form | legacy bytes | true-bytes form | new bytes | saving % |
|---|---|---|---|---|---|
| block_hash (v2, in `blocks` value) | 64-char hex string in msgpack `"h"` | 64 | raw blake2b-256 digest (`digest_size=32`, C2) | 32 | 50% |
| block_hash (v2) per-tx column, ×ntx | 64-char hex string per tx (retained at stored idx 6 today) | 64×ntx | dropped (block-level constant; re-synthesised at expanded idx 7) | 0 | 100% |
| `hashes` key (v2) | hex string encoded UTF-8 (`block_hash.encode()`) | 64 | raw 32-byte digest key (`bytes.fromhex`) | 32 | 50% |
| `hashes` value (v2) | u64 BE height (already raw, C1) | 8 | u64 BE height (unchanged, C1) | 8 | 0% |
| previous_hash | not stored (prior block's hash, reconstructed) | 0 | not stored (reconstructed, §3) | 0 | n/a |
| block height key | u64 BE (already raw, C1) | 8 | u64 BE (unchanged, C1) | 8 | 0% |
| per-block txid list (v2, NEW) | not stored (no forward list) | 0 | raw 32-byte txids ×n_postfork (C3) | 32×n | new index; 50% smaller than a naive 64-hex list |
| block_hash (LEGACY region, ALL mainnet today) | msgpack `"h"` hex + per-tx hex + hex `hashes` key | unchanged | **unchanged (FIX C: legacy on-disk byte-identity preserved)** | unchanged | 0% (intentional) |
| merkle root | none in Bismuth | 0 | none | 0 | n/a |


**Adversarial fixes folded in:**
- FIX C (on-disk byte-identity, FINDING C): Resolved in the STRICT direction. The legacy region (height < fork_height OR fork_height is None) keeps its EXISTING msgpack/hex on-disk encoding byte-for-byte — no re-encode, no new sub-db writes; the new raw-binary form applies ONLY to the v2 region (height >= fork_height). §1 'STRICT split', §6 write gating, and the savings-table 'LEGACY region (unchanged)' row spell this out. This preserves on-disk byte-identity for 100% of current mainnet blocks (all legacy today), keeps test_lmdb_on_disk_bytes_identical_to_direct_lmdb (tests/test_kvstore.py:114, tests/test_balance_index.py:96) green, and never force-rebuilds the 23 GB prod ledger.
- FIX B (reconstruction precedence, FINDING B): §3 pins len(raw_bytes) as the SOLE authoritative source for the reconstructed hex (32->64-hex, 28->56-hex). The fmt=0x04 marker and the height>=fork_height gate are explicitly demoted to advisory consistency checks that MUST NEVER reject a block or alter the reconstructed bytes; on disagreement the read still returns raw_bytes.hex() and logs (never raises). This kills the validate/apply desync risk a fork_height misconfiguration would otherwise create.
- FIX (_expand reinsertion index): §3 and §6 pin the synthesized block_hash at EXACTLY expanded 12-field index 7 (== stored 11-field index 6 after the [height] prepend): t.insert(7, reconstruct_block_hash(...)). Verified against block_store.py:211,215 (group[0][7]) and storage_backend.py:139 (rows[0][7]) so get_block rows and cross_check stay byte-for-byte SQLite-identical.
- FIX (txids filter parity): §2.3 enforces the IDENTICAL filter as txid_index.apply_rows (txid_index.py:75: skip h<=0 and h<fork_height), storing bytes.fromhex(txid_of(r, fork_height)) (raw-32, C3) from the same processor.block_transactions source used at digest.py:834. §5 adds the invariant test len(txids[height])//32 == post-fork positive-height tx count for straddling and post-fork blocks.
- FIX (rollback): §6 extends block_store.rollback (block_store.py:120-130) to (a) parse the raw 32-byte block hash out of the v2 binary record (bytes [1:33]) for the hashes delete-key when the value begins with 0x04 — NOT _unpack(v)['h'] — falling back to the legacy path otherwise, and (b) delete txids[height] in the SAME write txn. Adds a reorg test asserting no stale hashes/txids survive a rollback across the fork boundary.
- FIX (atomic hashes flip + snapshot rebuild): §6 and §7 make the v2-region hashes key flip (hex-as-UTF8 -> bytes.fromhex raw digest) and the block_already_exists (digest.py:874-888) read migration atomic, governed by the C8 env sentinel meta[b'fmt']=b'\x04' and the C7 per-projection flip; build_from_sqlite runs from a snapshot copy, never a hot full-scan of the live 23 GB ledger. The v2-region byte-identity characterization is re-baselined while the legacy-region characterization stays unchanged.
- FIX (straddling + None replay_verify + boundary cross_check): §5 requires both a fork_height=None replay_verify (0 mismatches, trivial since legacy bytes are unchanged) AND a straddling-chain replay_verify (0 mismatches across the boundary block), plus a storage_backend.cross_check(SqliteBackend, LmdbBackend) run that is byte-for-byte on reconstructed forms across the boundary (legacy 28-byte-digest/56-hex parent feeding a v2 32-byte-digest/64-hex child).
- A-hex compliance retained: every 'raw' claim (blocks-value block_hash, v2 hashes key, txids blob) is TRUE bytes in an LMDB key/value (C0). Hex appears ONLY in-memory on read via reconstruct_block_hash, never stored. blake2b widths pinned per C2 (digest_size=32 for block hash and txid). The legacy region's pre-existing hex-as-UTF8 hashes key is explicitly called out as A-hex but left UNCHANGED to satisfy FIX C (on-disk byte-identity) — the v2 region fixes it.


**Implementation checklist (residual review items to satisfy before wiring this domain):**
- [ ] Remove/correct the §2.1 ntx note: it MUST read 'positive-real-height tx count only' (no negative-height mirror rows), consistent with C1 and §7. block_store v2 records contain only the positive-height block body; reward mirrors are owned by reward_chain in a separate env.
- [ ] Extend the txids filter-parity requirement to the REBUILD/SNAPSHOT population path: build_from_sqlite (and any rebuild_from_cursor analogue that fills the txids sub-db) MUST apply the IDENTICAL filter (skip h<=0 and h<fork_height) and the IDENTICAL txid derivation (bytes.fromhex(tx_id_at(...))) as txid_index, so a snapshot-rebuilt block_store's txids blob is byte-identical to the apply-path one and the inverse-index invariant holds after restore — not only 'by construction' on the apply path.
- [ ] Fix the discriminant rationale in §1: make the destination-height gate the PRIMARY encoding selector and treat the value's leading byte as a redundant cross-check that is verified non-colliding against BOTH the msgpack map markers AND the JSON-fallback first byte '{' (0x7b), since Codec degrades to JSON without msgpack (kvstore.py:59-64).
- [ ] Correct the invariant-test expression in §2.3/§5 to coerce the height to int (e.g. `int(r[0]) > 0 and int(r[0]) >= int(fork_height)`); as written it raises TypeError because block_transactions r[0] is a string.
- [ ] Pin in §6 that the v2 recent_block_weights branch computes openfield weight from the reconstructed openfield STRING's character length (len(str(openfield_reconstructed))), reproducing the legacy `len(str(r[-1]))` value byte-for-byte, not from the raw v2 length prefix.

---

## 5. Coinbase compaction  `[coinbase]`

## hf2 Stage-4 — Coinbase Compaction (doc/29 §2.D, true-bytes LMDB form) — REVISED

> Obeys the SHARED CROSS-DOMAIN CONVENTIONS verbatim. In particular C0 (one fork
> signal, gate-by-destination-height, pre-fork byte-identity frozen, A-hex ban,
> replay-validated), C1 (`Codec.hkey` height key), C2 (blake2b digest_size pinned),
> C3 (canonical txid), C6 (codec split — `block_store` values are msgpack, never
> JSON), C7 (per-env layout / snapshots), C8 (format-version sentinel). Where this
> section and the shared section disagree, **the shared section wins.**

### Why this revision exists (the rejection)

The first version DROPPED `signature` and `public_key` from the compact coinbase
and reconstructed them as `''`. That is **not lossless against the frozen
post-fork block-hash pre-image** and breaks replay:

- `_v2_tx_bytes` (`bismuth_serialize.py:112-127`) is the frozen per-tx encoding the
  post-fork block hash consumes. Lines **124-125** encode `signature` and
  `public_key` **AS STORED** (still base64 — the explicit code comment at
  `bismuth_serialize.py:116-117` says the raw/drop refinement is "§2.C ... lands
  later"). So the post-fork `block_hash_v2` **commits to the coinbase sig+pubkey
  strings**. Dropping them changes the blake2b-256 block hash.
- `replay_verify.verify_blocks` (`replay_verify.py:64-66`) rebuilds each block hash
  from **stored rows**, feeding `str(r[SIG])` and `str(r[PUBKEY])` into
  `block_hash_at → block_hash_v2`. A `''`/`''` coinbase yields a different hash than
  the digester computed at `digest.py:622-626` from the real wire fields ⇒ non-zero
  mismatches on a straddling chain ⇒ violates the C0 "replay-validated, 0-mismatch"
  invariant.
- `storage_backend.cross_check` (`storage_backend.py:130-142`) asserts
  `got == rows` (line 138). The SQLite reference coinbase row holds the real RSA
  sig+pubkey strings; a `''`-reconstructed row fails the equality.

**Decision (settles old open-question #2 as a hard precondition):** Stage-4
coinbase compaction does **NOT** drop sig/pubkey. It stores them as **TRUE bytes**
(raw sig + the existing pubkey-dedup id) and `_expand` reconstructs the **exact
base64 strings** the block hash and the SQLite reference row contain — character
for character. Dropping sig/pubkey is deferred until doc/29 §2.C changes
`_v2_tx_bytes` to exclude/raw-encode them AND that change is itself
characterization-locked + replay-validated; only then may a Stage-5 follow-up drop
them, in lockstep on both the LMDB reconstruction and the post-fork SQLite
reference row. Stage-4 lands **after, or jointly with, §2.C only for the parts that
are already frozen**; it never assumes an unimplemented §2.C.

### What a coinbase is (ground truth)

The coinbase (mining reward) is the **last tx of every block** (`digest_tx.py:63`,
`index == tx_count - 1`). It is authorized by PoW + reward rules, never by its own
signature for value transfer (`digest.py:650` re-runs `check_block(..., new_pow=)`,
`digest.py:277-278` recomputes the reward). The stored 12-field row order is
(`digest.py:297-305`): `block_height, timestamp, address, recipient, amount,
signature, public_key, block_hash, fee, reward, operation, openfield`. For a
coinbase, the constant/derivable fields are:

- `recipient == address` (miner pays self; `miner.py:107,112` write `addr[:56]`
  to both).
- `amount == 0` (`digest_tx.py:64-65` rejects non-zero; reward is carried in the
  separate `reward` column, `digest.py:278`).
- `fee == 0` (`digest.py:279`).
- `operation == "0"` (`miner.py:107,113`).
- `address_is_rsa` is enforced (`digest_tx.py:66`) ⇒ 56-hex ASCII address.
- `block_hash` is the **row envelope key** (`block_store.py:114`, `{"h":…,"t":[…]}`)
  ⇒ not stored per-tx.
- `block_height` is the **LMDB key** (`Codec.hkey`, C1) ⇒ dropped already today.

Load-bearing fields that MUST round-trip byte-identically:
1. `timestamp` — feeds the block-timestamp gate (`digest.py:603`,
   `miner_tx.q_block_timestamp`) and the block-hash pre-image.
2. `miner_address` (== recipient) — committed into the PoW pre-image
   `address + openfield + blockhash` (`miner.py:87`) ⇒ cannot change without redoing
   PoW; also the payee.
3. `openfield` = `_coinbase_prefix + ('%0x' % nonce)` (`miner.py:81,86`) — the PoW
   pre-image input; must reconstruct character-for-character or `check_block` fails.
4. `signature` and `public_key` — **NOT dropped** (see rejection above); committed
   into `block_hash_v2` via `_v2_tx_bytes:124-125` as their stored base64 strings.
5. `reward` (column 9) — deterministic via `calculate_mining_reward(height)` + summed
   fees (`digest.py:277-278`), but is **stored explicitly** in this revision (see
   "reward" below).

### Compact record layout (TRUE bytes — LMDB value bytes, never hex-in-text)

The compact coinbase replaces the **last entry of the `block_store` `blocks` value
list** (`block_store.py:114`, the `t[]` array) with a **single opaque `bytes` blob**
instead of the 11-field list. Per C6 the block_store value is msgpack
(`Codec.pack`); a `bytes` member round-trips as `bytes` and a `list` member as
`list` (verified), which is the dispatch discriminator (see "Dispatch"). All
multi-byte integers little-endian, length-prefixed where variable — matching the
§2.A/§2.B convention (`bismuth_serialize._v2_lp`, `bismuth_serialize.py:72-74`).

```
COINBASE-V1 COMPACT  (one LMDB t[]-member, a msgpack bytes value)
off  field          type        width   notes
0    TAG            u8          1       0xC0  discriminator byte (belt-and-suspenders;
                                              the PRIMARY dispatch is isinstance(entry, bytes))
1    version        u8          1       0x01  hf2 compact coinbase
2    timestamp_cs   u64 LE      8       round(block_ts*100) = q_block_timestamp*100
10   addr_len       u8          1       56 for an RSA coinbase (digest_tx.py:66)
11   miner_address  ascii       addr_len  56-hex ASCII RSA address (== recipient)
..   sig_len        u16 LE      2       raw signature byte length (RSA-4096 = 512)
..   signature_raw  raw         sig_len  base64-DECODED signature bytes (NOT the 684-char b64)
..   pk_id          u64 LE      8       block_store pubkey-dedup id (pk/pkr, block_store.py:71-88)
..   reward_units   u64 LE      8       reward in atomic units (C4; u64 sufficient — BIS is bounded)
..   flags          u8          1       bit0: hf2 signal present in openfield prefix
                                        bit1: VM state-root present (vmsr)
..   [if bit1]      raw         32      vm_state_root: blake2b-256 (C2, digest_size=32), RAW 32 bytes
                                        (NOT "vmsr"+64-hex)
..   nonce          u64 LE      8       PoW nonce integer; openfield tail = ('%0x' % nonce)
```

Fixed overhead = 1+1+8+1 + 2 + 8 + 8 + 1 + 8 = **38 bytes** plus
`addr_len`(56) + `sig_len` raw signature(512 for RSA-4096) + optional 32-byte
VM root.

```
no VM root : 38 + 56 + 512                 = 606 bytes
with VM root: 38 + 56 + 512 + 32           = 638 bytes
```

Why each retained field is true-bytes, not text:
- **signature_raw** is the base64-**decoded** bytes (512 raw vs 684 base64 chars =
  a 1.34× shrink and an A-hex/A-base64 avoidance per C0). `_expand` reconstructs the
  exact base64 string via `base64.b64encode(signature_raw).decode()` — proven
  lossless: canonical `base64.b64encode` output is the unique no-newline encoding,
  so `b64encode(b64decode(s)) == s` for every miner-emitted sig.
- **public_key** is carried as the **8-byte `pk_id`** that `block_store` already
  assigns (`block_store.py:84-86`); the full RSA pubkey lives once in `pkr` and is
  shared with every other tx from the same miner. `_expand` does exactly what it
  does today for any row (`block_store.py:95-97`): `pkr[Codec.hkey(pk_id)].decode()`
  → the exact base64 pubkey string. This is **not new** dedup — it reuses the frozen
  `pk`/`pkr` machinery, so the reconstructed pubkey string is byte-identical to the
  legacy one. (The 11-field legacy stored row ALSO carries this id at index 5; the
  compact form simply moves it into the blob.)

The `recipient`, `amount`, `fee`, `operation` fields are omitted (constants
reconstructed below). The `block_hash`/`block_height` are omitted (envelope/key).

### Reconstruction rule (C-RECON, lossless → exact 12-field stored row)

`block_store._expand` (`block_store.py:90-99`, extended) rebuilds the canonical
12-field ledger row from a compact entry. The reconstructed row is **byte-identical**
to a legacy-stored coinbase row for every consensus pre-image and for `cross_check`.

```
height       = the LMDB key (Codec.unhkey, block_store.py:42)         # re-prepended
block_hash   = rec["h"]                                               # row envelope
timestamp    = '%.2f' % (timestamp_cs / 100)                          # exact: cs is integer centiseconds
address      = miner_address                                          # 56-hex ASCII
recipient    = miner_address                                          # coinbase pays self
amount       = '0'  if amounts.LEDGER_INTEGER else '0.00000000'       # C4 / digest.py:300 form
signature    = base64.b64encode(signature_raw).decode()              # EXACT base64 string (lossless)
public_key   = pkr[Codec.hkey(pk_id)].decode()                       # EXACT base64 pubkey (existing dedup)
fee          = '0'  if amounts.LEDGER_INTEGER else '0'                # digest.py:302, fee==0
reward       = str(reward_units) if amounts.LEDGER_INTEGER \
               else amounts.consensus_amount(reward_units)            # C4/C-RECON: from_units, NEVER display_amount
operation    = '0'                                                    # constant (miner.py:107)
openfield    = _coinbase_prefix_str(flags, vm_state_root) + ('%0x' % nonce)
```

`amount`/`fee`/`reward` reproduce the **exact string `digest.py:297-305` wrote**:
when `amounts.LEDGER_INTEGER` is set the stored fields are `str(to_units(x))`
(C0: `LEDGER_INTEGER` is a STORAGE flag, decoupled from `fork_height`), otherwise
the decimal strings. `_expand` reads `amounts.LEDGER_INTEGER` (`amounts.py:65`) — the
same flag the writer read — so the reconstructed strings match the legacy row in
both storage modes. `reward` round-trips through `amounts.consensus_amount`
(`amounts.py:65-72`), **never `display_amount`** (C4 / C-RECON), so it stays
byte-identical above 2⁵³ units.

**openfield reconstruction (the load-bearing part), exactly lossless:**
```
prefix = ("hf2" if flags.bit0 else "")                                # fork.FORK2_SIGNAL, fork.py:23
       + (vm_engine.embed_state_root(vm_state_root.hex(), "") if flags.bit1 else "")  # "vmsr"+64-hex
openfield = prefix + ('%0x' % nonce)
```
This reproduces `miner.py:81,86` and `_coinbase_prefix` (`miner.py:32-44`)
byte-for-byte: `FORK2_SIGNAL` ("hf2") then `vm_engine.embed_state_root` →
`"vmsr"+root` (`vm_engine.py:155-157`, `_ROOT_HEX=64`), then `'%0x' % nonce`. The
nonce round-trip is provably lossless because the miner itself produced the tail
with `'%0x'` (minimal, no-leading-zero hex): `'%0x' % int(s,16) == s` holds for
every value the miner can emit (verified: 0→"0", 5→"5", full 16-char width all
round-trip). The PoW pre-image `address + openfield + blockhash` (`miner.py:87`) is
therefore reconstructed character-identically, so `mining_heavy3.check_block(...)`
(`digest.py:650`) re-validates the same nonce.

**reward is stored explicitly** (`reward_units u64 LE`, +8 bytes) rather than
recomputed. Rationale (resolves old Risk #2): `calculate_mining_reward`
(`digest.py:223-233`) plus `sum(fees_block)` (`digest.py:278`) is the deterministic
*check* path, but the dev/HN mirror-reward path (`apply_rewards`,
`digest.py:398-408`) and `validation_exceptions`/trusted-prefix waivers
(`digest.py:602,646`) make recomputation fragile to rescue/irregular heights.
Storing the reward keeps `_expand` a pure decode (no recompute, no SQLite read on
the read path — C7 no-hot-scan) and keeps `cross_check` byte-identical to the
stored SQLite `reward` column. The validator's own `digest.py:277-278` recompute
stays the authoritative consensus check at digest time; storage just records what
was committed.

**Round-trip proof.** `_expand(_compact(row)) == row` for every field:
height/block_hash from envelope; timestamp_cs ↔ `'%.2f'` exact (integer
centiseconds); address/recipient = the same 56-hex; amount/fee = the same constant
string in the active storage mode; signature = `b64encode(b64decode(sig))` = sig;
public_key = `pkr[pk_id]` = the original (same dedup the legacy row used);
reward = the stored units rendered by the same `consensus_amount`/units rule the
writer used; operation = "0"; openfield = prefix + `'%0x'%nonce` = original. Every
mapping is a bijection on the values a valid coinbase can hold, so reconstruction
is total and lossless.

### Block hash & txid identity (now correct)

- **Block hash:** `block_hash_v2` (`bismuth_serialize.py:130-147`) consumes
  `_v2_tx_bytes` (`:112-127`), which at lines 124-125 encodes signature/public_key
  **as their stored base64 strings**. Because `_expand` reconstructs those exact
  strings, the post-fork block-hash pre-image over the reconstructed coinbase is
  byte-identical to the one the digester computed from the wire form
  (`digest.py:622-626`). replay over LMDB-read rows therefore reproduces the stored
  hash. (When §2.C later drops/raws sig+pubkey in `_v2_tx_bytes`, a Stage-5
  follow-up may then drop them here too — not before.)
- **txid:** `tx_id_v2_s` (`bismuth_serialize.py:159-163`, C3) is over the six
  logical content fields (sig/pubkey excluded by construction at `:84-90`), all of
  which `_expand` reconstructs exactly, so the content txid is unchanged. Per C3 the
  canonical txid byte form in any key/dedup store is raw-32; the coinbase's txid is
  resolvable from the reconstructed row.

### Dispatch / fork gate (file:line)

The compact form is chosen **only when writing a post-fork block**, keyed on the
destination height — never a global mode (C0).

- **Gate site (write):** `block_store.put_blocks` (`block_store.py:102-115`) is the
  single place the per-tx stored form is built. The last row of a block whose
  `height >= node.fork_height` is encoded as the compact blob **iff** the
  write-side recognizer (below) accepts it; every pre-fork block (and
  `fork_height is None`, i.e. mainnet today) writes the existing 11-field list
  unchanged. `fork_height` is threaded into the store (or passed per put-call) from
  the same `node.fork_height` the block-hash gate reads (`digest.py:624-625`). No
  second signal; `amounts.LEDGER_INTEGER` stays a STORAGE flag (C0).
- **Builder gate (miner):** `miner._build_block` (`miner.py:99-114`) keeps emitting
  the full 8-field coinbase tuple onto the wire; compaction is purely a `block_store`
  storage transform, so the wire/SQLite reference form is unchanged and
  `cross_check` (`storage_backend.py:130-142`) stays meaningful. Wire-level coinbase
  compaction is **out of scope** for Stage-4.
- **Dispatch on read (TYPE-based, per required fix):** `block_store._expand`
  (`block_store.py:90-99`) checks **`isinstance(t, (bytes, bytearray))`** for each
  `t[]` member → compact-coinbase decode; **`list`** → the existing 11-field path.
  This is correct under BOTH the msgpack codec and the JSON-fallback codec (C6,
  `kvstore.Codec`), because a `bytes` member round-trips as `bytes` and a list as a
  list (verified). The `0xC0` leading byte is retained as a cheap version/format
  sanity assert inside the bytes branch, not as the discriminator.
- **`None`-means-legacy / pre-fork byte-identity (C0):** with `fork_height is None`
  (mainnet today) `put_blocks` takes the legacy branch for 100% of blocks; the
  stored `t[]` is the identical 11-field list, `_expand` never sees a bytes member,
  and the frozen `signature_buffer`/`tx_id`/`block_hash` + v2 siblings
  (`bismuth_serialize.py:23-55,77-147`) are untouched. `storage_backend.cross_check`
  stays byte-for-byte because the reconstructed 12-field row equals the SQLite row
  (including the real sig/pubkey base64 strings — no `''`).

### Write-side recognizer (MANDATORY precondition, resolves old Risks #3/#4)

`put_blocks` compacts the last row **only if it is a canonical hf2 coinbase**;
otherwise it falls back to the legacy 11-field list. The recognizer requires ALL of:
1. it is the last row AND `height >= fork_height`;
2. `recipient == address`, `amount`-units == 0, `fee`-units == 0, `operation == "0"`;
3. `address_is_rsa(address)` (56-hex), matching `digest_tx.py:66`;
4. the openfield decomposes **exactly** as `[hf2?][vmsr+64hex?] + tail` where
   `tail` satisfies `'%0x' % int(tail,16) == tail` (the nonce round-trip guard —
   rejects any leading-zero-padded or non-minimal hex a non-conforming GPU kernel
   might emit);
5. nothing else remains in the openfield (no third-party extra bytes).
Any failure ⇒ legacy list encoding. This makes reconstruction provably lossless
and PoW re-verification exact for every compacted coinbase. (Whether consensus
should *require* the canonical openfield shape post-fork — making compaction
mandatory and dropping the fallback — is a consensus-scope question, deferred.)

### Code that changes (file:line)
- `block_store.py:102-115` (`put_blocks`) — add the recognizer + post-fork compact
  encode for the last row; thread `fork_height` into the store.
- `block_store.py:90-99` (`_expand`) — TYPE-based dispatch
  (`isinstance(t, (bytes, bytearray))`) + the 12-field reconstruction above
  (sig via `b64encode`, pubkey via existing `pkr` lookup, openfield + reward rebuild).
- `block_store.py:141-157` (`recent_block_weights`) — `openfield` length for a
  compact coinbase must be the **reconstructed** string length (fee/weight
  determinism); add a compact-aware length (decode flags+nonce, compute
  `len(prefix)+len('%0x'%nonce)`) so `ofbytes` matches the legacy row.
- `vm_engine.py:155-169` (`embed_state_root`/`extract_state_root`) — reused as-is
  for the `vmsr`+hex reconstruction (root stored raw, re-hexed on read).
- `digest.py:669-694` (state-root enforcement) reads `extract_state_root(_t[11])`
  on the **expanded** openfield — unchanged once `_expand` reconstructs `openfield`.
- `tests/test_characterization.py` — `test_coinbase_compact_roundtrip_is_lossless`:
  pin the 606/638-byte layout AND assert the expanded row `== ` a legacy-stored
  coinbase row field-for-field (incl. real sig/pubkey strings).
- `tests/test_replay.py` — extend with a **straddling-chain** case: write a
  post-fork range through `block_store` (compact coinbases), read rows back via
  `block_store.blocks_in_range`/`get_block`, run
  `replay_verify.verify_blocks(rows, fork_height=fh)` and require **0 mismatches**
  (this is the gate that the first version would have failed).
- `storage_backend.py:130-142` — no code change required; `cross_check` now passes
  because `got == rows` holds (sig/pubkey reconstructed identically).

### Per-env / sentinel / flip (C7, C8)

`block_store` is the canonical append-only env (C7); compaction is an on-disk format
change confined to the v2 region (`height >= fork_height`), so the legacy 23 GB
region is never force-rebuilt (C0). The env carries the C8 1-byte format sentinel
`meta[b"fmt"] = b"\x04"`; an env opened without it (or lower) refuses to serve
consensus reads until rebuilt-from-snapshot, so an old node's hex/list-only blocks
never silently mis-decode. Per C7 the SQLite→LMDB consensus-read flip for the block
surface is gated: run `block_store.verify_against_sqlite` + `storage_backend.cross_check`
in shadow (`parity_strict`) and require `replay_verify` 0-mismatch over a straddling
chain before `*_consensus` flips to primary.


**Savings**

| field | legacy form | legacy bytes | true-bytes form | new bytes | saving % |
|---|---|---|---|---|---|
| TAG + version | (none) | 0 | u8 tag 0xC0 + u8 version | 2 | — (new framing) |
| block_height | str(int) TEXT | ~8 | dropped (LMDB key, `Codec.hkey`) | 0 | 100% |
| timestamp | `'%.2f'` e.g. "1718000000.00" | 13 | u64 LE centiseconds | 8 | 38% |
| address (miner) | 56-hex ASCII | 56 | u8 len + 56 ASCII | 57 | -2% (len prefix) |
| recipient | 56-hex ASCII (== address) | 56 | dropped (== miner_address) | 0 | 100% |
| amount | `'0.00000000'` (or `'0'` int-mode) | 10 | dropped (constant 0) | 0 | 100% |
| signature | base64 RSA PKCS#1v1.5 | ~684 | u16 len + RAW sig bytes (b64-decoded) | 514 | 25% |
| public_key | base64 RSA pubkey (raw ~1068) | ~1456 | u64 pk_id (existing pk/pkr dedup) | 8 | 99.5% |
| block_hash | 56-hex ASCII | 56 | dropped (row envelope key) | 0 | 100% |
| fee | `'0.00000000'` (or `'0'` int-mode) | 10 | dropped (constant 0) | 0 | 100% |
| reward | `'%.8f'`/int reward string | ~12 | u64 LE reward_units | 8 | 33% |
| operation | `'0'` | 1 | flags bit (signal) | 0 (in flags) | 100% |
| flags | (implicit openfield substring scan) | 0 | u8 | 1 | — (new) |
| openfield: hf2 signal | `"hf2"` literal | 3 | flags.bit0 | 0 | 100% |
| openfield: vmsr root | `"vmsr"+64-hex` | 68 | flags.bit1 + 32 raw bytes | 32 | 53% |
| openfield: nonce | `'%0x'` hex (≤16 chars) | ~16 | u64 LE nonce | 8 | 50% |
| **TOTAL (no VM root)** | full legacy coinbase row | **~1893** | compact blob | **606** | **68.0%** |
| **TOTAL (with VM root)** | full legacy + vmsr | **~1961** | compact + 32B root | **638** | **67.5%** |


**Adversarial fixes folded in:**
- REQUIRED FIX (do not drop sig/pubkey until §2.C lands): RESOLVED — the compact coinbase now RETAINS both. The block hash via _v2_tx_bytes (bismuth_serialize.py:124-125) commits to sig+pubkey AS STORED today, so Stage-4 keeps them and reconstructs the exact base64. Dropping is explicitly deferred to a Stage-5 follow-up that must land after/jointly with the characterization-locked, replay-validated §2.C change, and in lockstep on the SQLite reference row.
- REQUIRED FIX (store sig/pubkey as TRUE bytes, reconstruct exact base64 not ''): RESOLVED — signature is stored as u16 len + base64-DECODED raw bytes (512 for RSA-4096) and reconstructed via base64.b64encode(...).decode() (proven lossless: canonical no-newline b64); public_key is carried as the existing block_store pk_id (pk/pkr, block_store.py:71-88) and reconstructed via pkr[Codec.hkey(pk_id)].decode() — both yield the exact original base64 strings, character-for-character. No '' anywhere.
- REQUIRED FIX (straddling-chain replay test BEFORE shipping, 0 mismatches): RESOLVED — added a tests/test_replay.py case that writes a post-fork range through block_store (compact coinbases), reads rows back via block_store, and asserts replay_verify.verify_blocks(rows, fork_height=fh) == [] (0 mismatches). This is exactly the gate v1 would have failed because it fed ('','') into block_hash_v2 (replay_verify.py:64-66).
- REQUIRED FIX (settle open-question #2: SQLite ref row == LMDB reconstruction, byte-for-byte, or cross_check breaks): RESOLVED — both keep reconstructable sig/pubkey for Stage-4: the LMDB _expand reproduces the same base64 sig+pubkey the SQLite row holds, so storage_backend.cross_check's got == rows (storage_backend.py:138) and block_hash assert (line 139) both pass with no code change. The drop-in-lockstep alternative is explicitly deferred to Stage-5 + §2.C, not left unsettled.
- REQUIRED FIX (read dispatch as TYPE check, not 0xC0 first-byte peek): RESOLVED — _expand dispatches on isinstance(t, (bytes, bytearray)) -> compact, list -> legacy (verified to round-trip distinctly under both the msgpack and JSON-fallback Codec, kvstore.py). The 0xC0 byte is demoted to an in-branch version/sanity assert, not the discriminator.
- REQUIRED FIX (keep the third-party-openfield recognizer + nonce '%0x' round-trip guard as MANDATORY write-side preconditions): RESOLVED — put_blocks compacts the last row ONLY if it passes a strict recognizer (last row & height>=fork_height; recipient==address; amount/fee units==0; operation=='0'; address_is_rsa per digest_tx.py:66; openfield decomposes EXACTLY as [hf2?][vmsr+64hex?] + tail with '%0x'%int(tail,16)==tail and nothing left over). Any failure falls back to the legacy 11-field list, guaranteeing lossless reconstruction and exact PoW re-verification.


**Implementation checklist (residual review items to satisfy before wiring this domain):**
- [ ] Fix reward reconstruction in _expand: use `reward = str(reward_units) if amounts.LEDGER_INTEGER else amounts.from_units(reward_units)` (NOT amounts.consensus_amount). consensus_amount is identity in legacy mode and returns the integer units, breaking byte-identity of the stored decimal-string reward; from_units(reward_units) reproduces the exact '{:.8f}' string digest.py:303 stored. Re-run the straddling-chain replay test AND storage_backend.cross_check with amounts.LEDGER_INTEGER=False (mainnet-style default) AND =True, requiring 0 mismatches in BOTH modes before any *_consensus flip.
- [ ] Add a base64 canonicality precondition to the write-side recognizer: compact the coinbase ONLY if base64.b64encode(base64.b64decode(signature)).decode() == signature (the exact stored string); otherwise fall back to the legacy 11-field list. This makes the b64decode->store-raw->b64encode round-trip provably lossless and keeps block_hash_v2 byte-identical even for a non-canonically-encoded coinbase signature.
- [ ] Replace the float timestamp reconstruction with integer arithmetic: `timestamp = '%d.%02d' % divmod(timestamp_cs, 100)` so the '%.2f' string is reproduced exactly at every magnitude without float64 (provably the inverse of _v2_ts_cs). Pin it in the characterization round-trip vector.
- [ ] State explicitly that put_blocks recomputes pk_id via _pubkey_id within the same write txn when building the compact blob (never embeds a stale id), and add a rebuild/reorg test that drops+rebuilds the env from a snapshot and asserts every compact coinbase's reconstructed pubkey still matches the SQLite reference (dedup-id stability under re-insertion order).

---

## 6. VM state root + contract state  `[vm]`

## hf2 Stage-4 TRUE-BYTES storage — domain: VM (state-root carrier + contract state)

> Obeys the SHARED CROSS-DOMAIN CONVENTIONS verbatim. Where this section and the shared section disagree, the shared section wins. C-references below point at it: C0 (governing invariants), C2 (blake2b width), C3 (txid), C5 (address form), C7 (per-env / state-root / migration flip), C8 (format sentinel).

### 0. Scope and what is ALREADY true bytes

Two distinct surfaces carry "VM" bytes. They are NOT equally safe to change, and the adversarial review proved exactly why.

1. **Surface (A) — the consensus commitment of the VM pre-state root as it rides in the coinbase.** Today it is the ASCII string `"vmsr" + 64-hex` produced by `vm_engine.embed_state_root` (`vm_engine.py:155-157`), prepended into the mined coinbase openfield by the miner (`miner.py:40-43`) and regnet (`regnet.py:125-128`), and re-extracted + string-compared by the digester (`digest.py:672-694`). This is a true A-hex regression in the wild: a 32-byte blake2b-256 digest spelled as 64 hex chars plus a 4-char marker = 68 ASCII bytes in a TEXT field for 32 bytes of entropy (2.13x). **Surface (A) is SOUND and ships as the Stage-4 VM deliverable.**

2. **Surface (B) — the contract-state KV store** in `vm_state.py` (sub-DBs `code`/`storage`/`balances`). This is **already TRUE bytes** in the LMDB value AND key space: storage keys are `(addr+":").encode() + word.to_bytes(32,"big")`, storage values `int(val).to_bytes(32,"big")`, balances `bal.to_bytes(32,"big")`, code raw `bytes(code)` (`vm_state.py:45,58-71,92`). No Codec, no hex, no JSON (C6 "Raw bytes — no codec" row). The only remaining waste is the per-slot `addr+":"` text key-prefix (56 ASCII hex + 1 separator = 57 bytes for a 28-byte address). **Folding that prefix is the part the review found fatal, and it does NOT ship in Stage-4 by default** (see §B). It is specified here only as a gated, optional store-internal optimization that is shippable *only* with the mandatory re-sort fix (§B.2) and its characterization test (§C), because it touches the order-dependent consensus COMMITMENT.

So Stage-4 VM = **(A) ship the raw-32 root carrier** (replace `"vmsr"+64hex` with 32 raw bytes inside the compact coinbase, reconstructing the exact `"vmsr"+64hex` / 64-hex form on read wherever frozen consensus or text consumers expect it); **(B) is held back** unless the re-sort lands.

---

### A. VM pre-state root: 32 raw bytes in the compact coinbase (SHIPS)

#### A.1 Where the 32 bytes live

`state_root()` returns `blake2b(digest_size=32).hexdigest()` (`vm_state.py:104,119`; C2 row "Per-env projection state-root" / "TX id" both pin width **32**) — so it is **definitionally** 32 raw bytes rendered as 64 lowercase hex. `node.vm_state_root` is that hex string (set at `digest.py:788`, `chain_ops.py:83`). The raw 32 bytes are `bytes.fromhex(node.vm_state_root)`.

In the Stage-4 compact coinbase (doc/29 §2.D) the root is a field of the compact-coinbase record, carried as **32 raw bytes** behind a flag bit, never as `"vmsr"+hex`:

```
# Compact coinbase record (last tx of a post-fork block) — little-endian
field             | type        | width      | notes
------------------+-------------+------------+-----------------------------------------
version           | u8          | 1          | 0x01 = hf2 compact coinbase
timestamp_cs      | u32 LE      | 4          | round(block_ts*100), integer centiseconds
addr_len          | u8          | 1          | == 56 for an RSA-hex coinbase address
miner_address     | bytes       | addr_len   | ASCII '0-9a-f' (56-hex RSA address)
flags             | u8          | 1          | bit0: hf2 readiness signal
                  |             |            | bit1: vm_state_root present
vm_state_root     | bytes       | 32         | PRESENT iff flags.bit1 — raw blake2b-256 pre-state root
nonce             | u64 LE      | 8          | PoW nonce as integer
```

Root carrier: 1 flag bit + (when set) 32 raw bytes, vs 68 ASCII bytes today. The `vm_state_root` field is raw 32 bytes in the LMDB block_store value's compact-coinbase record — true bytes, NOT hex (C0 A-hex ban satisfied).

```
# Legacy openfield VM-root carrier (vm_engine.embed_state_root) — ASCII TEXT field, FROZEN (untouched)
field   | type   | width
--------+--------+------
"vmsr"  | ASCII  | 4      # COINBASE_ROOT_MARKER (vm_engine.py:151)
root    | ASCII  | 64     # 64 lowercase hex of the 32-byte digest
(rand)  | ASCII  | var    # PoW entropy concat, separate concern (miner.py:86)
```

#### A.2 Reconstruction rule (C-RECON — round-trip proof)

The digester's check at `digest.py:680/691` does **string equality of two 64-hex digests**: `_claimed = vm_engine.extract_state_root(coinbase_openfield)` vs `_local = node.vm_state_root`. Reconstruction is therefore total and exact:

```
raw32        = coinbase_record.vm_state_root          # 32 bytes, present iff flags.bit1
claimed_hex  = raw32.hex()                            # 64 lowercase hex == what extract_state_root returns
# legacy openfield string form, for any text consumer that still wants it:
openfield_vmsr = vm_engine.COINBASE_ROOT_MARKER + raw32.hex()   # == embed_state_root(node.vm_state_root, "")
```

**Round-trip proof.** `state_root()` emits lowercase hex; `bytes.fromhex` and `.hex()` are exact inverses on lowercase hex. Therefore for every node, every height:
`state_root_from_bytes(state_root_to_bytes(node.vm_state_root)) == node.vm_state_root` (identity), and `claimed_hex == _local` iff the raw 32 bytes equal the local root's 32 bytes — **bit-for-bit the same decision `digest.py:691` makes today.** No information is added or lost across the carrier swap.

Add a thin decoder **beside** the frozen functions; NEVER mutate `embed_state_root`/`extract_state_root` (C0 pre-fork byte-identity):

```python
# vm_engine.py — ADD beside COINBASE_ROOT_MARKER / embed_state_root / extract_state_root (the frozen three)
def state_root_to_bytes(root_hex: str) -> bytes:
    "32 raw bytes of a 64-hex state root, for the compact-coinbase carrier."
    return bytes.fromhex(root_hex)

def state_root_from_bytes(raw32: bytes) -> str:
    "Reconstruct the 64-hex root from the compact-coinbase 32 raw bytes (== extract_state_root form)."
    return raw32.hex()

def extract_state_root_compact(coinbase_record) -> "str|None":
    "Committed root from a compact coinbase: 64-hex (== legacy extract_state_root form) or None."
    if not (coinbase_record.flags & 0x02):            # bit1: vm_state_root present
        return None
    return coinbase_record.vm_state_root.hex()
```

The digester's mandatory-root check becomes carrier-agnostic: pick `_claimed` by the coinbase's *form* (legacy 12-field tuple → `vm_engine.extract_state_root(_t[11])`; compact record → `extract_state_root_compact(rec)`), then compare the resulting 64-hex against `node.vm_state_root` **exactly as `digest.py:685-694` does now**. The `_claimed is None` mandatory-root rule (and its raise) and the mismatch raise are unchanged.

**Text consumers keep the 64-hex (C-RECON / C0 A-hex ban).** `rest_api.py:877` returns `node.vm_state_root` (64-hex string) in `/api/vm/contracts`; `pool/optipoolware.py:109-112` reads that `state_root` and feeds it to `vm_engine.embed_state_root(sr, "")` to build its mining prefix. Both MUST keep receiving the reconstructed 64-hex via `state_root_from_bytes`, NEVER the raw 32 bytes — else external explorers/pools break. The raw 32 bytes live only inside the LMDB block_store value's compact-coinbase record.

#### A.3 Fork gate (by DESTINATION height) and pre-fork byte-identity (C0)

- The compact coinbase, and therefore the raw-32 carrier, exists **only** on the post-fork serialization path, which is already destination-height gated on the **one** signal `node.fork_height` (C0 "one fork signal"): the digester enforces only when `_efh is not None and block_instance.block_height_new >= _efh` (`digest.py:674`); the miner embeds a root only when `fh is not None and (node.last_block + 1) >= fh` (`miner.py:41`), same gate in regnet (`regnet.py:125-128`). No second flag; `amounts.LEDGER_INTEGER` is irrelevant here (no amount is stored in the carrier).
- **Pre-fork (`fork_height is None`, mainnet today) is byte-identical by construction:** the compact-coinbase encoder/decoder is never reached; the coinbase stays the legacy 12-field RSA row and the root, if present, stays the `"vmsr"+64hex` openfield emitted by the **unmodified** `embed_state_root`. Every historical/current block re-serializes through the exact same legacy code. The frozen three (`embed_state_root`/`extract_state_root`/`COINBASE_ROOT_MARKER`) are not mutated.
- **Boundary symmetry:** the *decoder* is chosen by the coinbase record's `version`/form (which exists only at/after `fork_height`), never a global mode; the *comparison value* `node.vm_state_root` is the same 64-hex domain on both sides. A straddling chain compares correctly at every height — pre-fork via `extract_state_root`, post-fork via `extract_state_root_compact`.

**REQUIRED FIX — fork.has_fork_signal must read flags.bit0, not a substring scan.** `fork.has_fork_signal` (`fork.py:29-31`) is today `FORK2_SIGNAL ("hf2") in str(openfield)` — a substring scan over the ASCII openfield. A compact coinbase has **no ASCII openfield**; the hf2 readiness signal moves to `flags.bit0` of the compact record. The signal-counting readers (`fork.db_fork_signal_reader` `fork.py:154-165`, feeding `dynamic_fork_height`/`lockin_at_tip`) MUST, for a height whose coinbase is a compact record, read `bit0` from the decoded `flags` byte instead of scanning the openfield substring. Concretely: `has_fork_signal` gains a compact-aware path —

```python
# fork.py — has_fork_signal becomes form-aware (compact coinbase has no ASCII openfield)
def has_fork_signal(coinbase):
    rec = compact_coinbase_or_none(coinbase)          # bismuth_serialize decoder; None for legacy 12-field
    if rec is not None:
        return bool(rec.flags & 0x01)                 # bit0: hf2 readiness
    return bool(coinbase) and FORK2_SIGNAL in str(coinbase)   # legacy: unchanged substring scan
```

Otherwise window/lock-in counting (`fork.py:45-74,137-151`) misreads every post-boundary block and signal accounting breaks at the boundary. This is a Stage-4 required fix, not an open item.

---

### B. Contract-state storage keys: `addr+":"` prefix fold — HELD BACK (gated; ships ONLY with the re-sort)

#### B.0 Why it does not ship by default

The store is **already true bytes** (§0.2); the only waste is the 57-byte `addr+":"` key prefix per slot. Folding it to a fixed 32-byte `cid` is a *store-internal* win — but `state_root()` (`vm_state.py:97-119`) is the consensus COMMITMENT and is **order-dependent**: it feeds slots into a single sequential blake2b accumulator in **on-disk key order**. `txn.iterate(db)` returns ascending byte-lexicographic key order on BOTH backends — LMDB `cur.first()` + `iternext` (`kvstore.py:217-230`) and sqlite `ORDER BY key` (`kvstore.py:345-359`). Folding the on-disk key from `addr.encode()+":"+word` (ASCII-address-major) to `cid+word` (blake2b-hash-major) is a **different permutation of the same multiset** of slots and of contracts → a different accumulation sequence → a **different root** → a network fork. Reconstructing each slot's pre-image bytes in the loop is **necessary but NOT sufficient**: the emission *sequence* must also match. Per the last required fix, the safe Stage-4 default is to **NOT fold (B)**; ship (A) alone. (B) is specified below for a future stage and is shippable only with §B.2 + §C.

#### B.1 The (gated) key layout

```
# storage_db key — CURRENT (already true bytes; vm_state.py:58,69)
field    | type  | width | total
---------+-------+-------+------
addr     | ASCII | 56    |
":"      | ASCII | 1     | 89 bytes/slot
word_key | bytes | 32    |
```
```
# storage_db key — folded (store-internal, GATED on the §B.2 re-sort)
field    | type  | width | total
---------+-------+-------+------
cid      | bytes | 32    | 64 bytes/slot   # cid = blake2b(addr.encode(), digest_size=32)  — C2 dedup-fold width 32
word_key | bytes | 32    |                 # unchanged, BE 256-bit word
```

`cid = blake2b(addr.encode(), digest_size=32)` (C2 — pinned width **32**, mirroring `block_store.pk` `block_store.py:76-79`; resolves open-question #1 in favour of 32 for uniformity with the rest of the LMDB stores). `code`/`balances` keys move the same way: `cid` (32B) instead of `addr.encode()` (56B). Values are unchanged (C6 "Raw bytes — no codec" — already raw 32-byte BE words / raw bytecode). The fixed-width `cid` removes the `":"` separator entirely (a fixed-width prefix cannot alias a longer key), and `load_storage`'s prefix scan (`vm_state.py:56-63`) becomes a fixed 32-byte prefix. This is a binary-key-space win, not an A-hex move (56 ASCII + 1 → 32 raw bytes).

#### B.2 MANDATORY re-sort — `state_root()` must collect → reconstruct legacy keys → SORT → hash

The COMMITMENT pre-image (`vm_state.py:97-119`) MUST stay byte-identical. Folding the on-disk key alone changes emission order and forks. The fix is a full **collect-reconstruct-SORT** pass per root computation, in the legacy full-key byte order:

```python
def state_root(self):
    import hashlib
    # 1. cid -> addr from code_db (code value carries addr, §B.3), so the legacy keys are recoverable.
    cid2addr = {}
    with self.store.txn() as txn:
        for cid, val in txn.iterate(self.code_db):
            addr, code = self._split_code_value(val)         # u8 addr_len || addr || code  (§B.3)
            cid2addr[cid] = addr
    # 2. collect every slot, REBUILD the legacy full-key bytes, SORT by them (== legacy on-disk order).
    code_items, stor_items, bal_items = [], [], []
    with self.store.txn() as txn:
        for cid, val in txn.iterate(self.code_db):
            addr, code = self._split_code_value(val)
            code_items.append((addr.encode(), code))                          # legacy key = addr.encode()
        for k, v in txn.iterate(self.stor_db):
            addr = cid2addr[k[:32]]; word = k[32:]
            stor_items.append(((addr + ":").encode() + word, v))             # legacy key = addr.encode()+b":"+word
        for cid, bal in txn.iterate(self.bal_db):
            bal_items.append((cid2addr[cid].encode(), bal))                   # legacy key = addr.encode()
    code_items.sort(); stor_items.sort(); bal_items.sort()                    # byte-sort == legacy iterate order
    # 3. emit in the EXACT legacy sequence and pre-image (byte-identical to vm_state.py:106-118 today).
    h = hashlib.blake2b(digest_size=32)                                       # C2 width 32
    for legacy_key, code in code_items:
        h.update(b"C"); h.update(legacy_key); h.update(len(code).to_bytes(4, "big")); h.update(code)
    for legacy_key, v in stor_items:
        h.update(b"S"); h.update(legacy_key); h.update(v)
    for legacy_key, bal in bal_items:
        h.update(b"B"); h.update(legacy_key); h.update(bal)
    return h.hexdigest()
```

**Round-trip proof.** The legacy store hashes `(b"C"+addr.encode()+len+code)`, `(b"S"+addr.encode()+b":"+word+v)`, `(b"B"+addr.encode()+bal)` in ascending legacy-key order. The folded store reconstructs the **identical legacy_key bytes** (cid→addr from code value) AND re-sorts by them, so both the per-slot pre-image AND the emission sequence are byte-identical across (a) multiple contracts whose addresses interleave lexicographically and (b) any reorg/rebuild. `code_items.sort()` over `addr.encode()` matches the legacy code_db order; `stor_items.sort()` over `addr.encode()+b":"+word` matches the legacy storage cross-contract interleave exactly; `bal_items.sort()` over `addr.encode()` matches the legacy balances order. Therefore `state_root()` over the folded store == `state_root()` over the legacy addr-prefixed store, bit-for-bit. This keeps the COMMITMENT frozen (no new pre-image), so `digest.py:691` enforcement, `replay_verify` (0-mismatch over a straddling chain, C0/C7), and the LMDB-vs-sqlite-kv parity all hold. **Open-question #2 is resolved in writing: NO new root pre-image — keep the frozen `addr:word` pre-image AND additionally specify this re-sort, since keeping the pre-image frozen is necessary but NOT sufficient without ordering fidelity.**

The `load_storage`/`commit_storage` `{int:int}` reconstruction is by construction: the VM only ever sees `int.from_bytes(k[len(prefix):], "big")` (the trailing 32-byte word, `vm_state.py:62`); with `prefix = cid (32B)`, `k[32:]` is the identical word, so the returned `{int:int}` dict is unchanged.

#### B.3 Code sub-DB carries the address (cid→addr reconstruction)

`cid = blake2b(addr)` is one-way, so the store MUST retain `addr` to (i) reconstruct the frozen `state_root` pre-image (§B.2), and (ii) satisfy `list_contracts()` / `storage_items()` (`vm_state.py:51-53,75-77`) which currently decode `addr` from the key. Fold it into the `code` value (small, immutable per deploy):

```
# code_db value — folded
field      | type  | width    | notes
-----------+-------+----------+----------------------------------
addr_len   | u8    | 1        |
addr       | bytes | addr_len | contract address ASCII (56) — cid->addr + display
code       | bytes | rest     | raw bytecode, unchanged
```

`get_code(addr)` strips the `u8 addr_len || addr` header; `deploy(addr, code)` prepends it; `list_contracts()` reads `addr` from each value (not the key). Cost: ~57 bytes once per contract — negligible against the 25-byte/slot key saving across thousands of slots. **This carry MUST land in the same change as the key fold**, or the store cannot rebuild its own root. C5 note: address keys here move to raw `cid`, but `addr` (the base58/hex string) is reconstructed losslessly before any consensus/dispatch use — storage-raw, dispatch-string.

#### B.4 Fork gate and migration (C7)

The vm_state contract store is **post-fork-only** (`vm_state.py:9-13`; opened only when `fork_height` is set; canonical record = the chain's `vm:` txs) and carries **no pre-fork bytes** (no `vm:` txs exist pre-fork; the store is empty/absent on mainnet today). So the key-layout fold is transitively gated by `node.fork_height` via the store's post-fork existence and needs no per-height branch inside `vm_state.py`. Migration is a single `clear()` + `vm_engine.rebuild(...)` (`vm_engine.py:172-181`, driven from `chain_ops.py:83` / startup `node.py` / reorg `chain_ops.py:481`) — it re-materializes every slot under the folded layout from the same `vm:` txs deterministically; **no standalone migration tool** (resolves open-question #4). Per C7, vm_state lives in its **own env** (separate from block_store and from reward_chain's negated keyspace), the rebuild runs **from a snapshot copy, never a hot full-scan of the 23 GB prod ledger** (C0), and the env carries the C8 `meta[b"fmt"]=b"\x04"` sentinel: an operator-copied pre-Stage-4 vm_state env (old `addr+":"` keys) opens with a lower/absent sentinel and **force drop+rebuilds** rather than silently missing every `cid`-keyed lookup. The C7 per-env state-root for vm_state IS `state_root()` itself (the template the shared section cites at `vm_state.py:97`).

---

### C. Replay / cross-check obligations & required tests

- **replay_verify (C0):** 0 mismatches with `fork_height=None` (legacy `embed/extract_state_root` only; compact coinbase never built) AND 0 across a straddling regnet chain (post-fork blocks decode the raw-32 carrier to the identical 64-hex compared at `digest.py:691`). For surface (A) this is exact by §A.2's identity round-trip.
- **storage_backend.cross_check / block_store.verify_against_sqlite (C0):** unaffected by (A) (the carrier rides in the block_store value's compact-coinbase record; reconstruction yields the byte-identical openfield/64-hex).
- **(A) new vectors:** prove `state_root_from_bytes(state_root_to_bytes(root)) == root` for arbitrary 64-hex roots, and `extract_state_root_compact(rec) == extract_state_root(embed_state_root(root))` for a record built from `root`. Leave `tests/test_vm_state.py` round-trips of the frozen `embed_state_root`/`extract_state_root` untouched.
- **(A) fork-signal vector:** prove `has_fork_signal(compact_rec_with_bit0)` is True and `has_fork_signal(compact_rec_without_bit0)` is False, and that `dynamic_fork_height`/`lockin_at_tip` count a straddling chain of compact coinbases identically to the legacy substring path.
- **(B) BLOCKING characterization test (must pass BEFORE any (B) flip):** prove `state_root()` over the `cid`-folded store equals `state_root()` over the frozen `addr`-prefixed layout **byte-for-byte** across (a) multiple contracts whose addresses interleave lexicographically (so the cross-contract interleave is exercised) and (b) a reorg/rebuild. The existing LMDB==sqlite-kv parity (`tests/test_vm_state.py`) is INSUFFICIENT — both backends would share the same wrong order; the test must compare the folded layout against the frozen addr-prefixed layout directly.

---

### D. Code sites that change (file:line)

- `vm_engine.py:151-169` — **ADD** `state_root_to_bytes` / `state_root_from_bytes` / `extract_state_root_compact` **beside** the frozen `COINBASE_ROOT_MARKER` / `embed_state_root` / `extract_state_root` (do NOT mutate the frozen three). (A)
- `bismuth_serialize.py` (doc/29 §2.D, beside `_v2_tx_bytes` at `:112-147`) — **ADD** the compact-coinbase encoder/decoder (`version|ts_cs|addr_len|addr|flags|[root]|nonce`); raw-32 root is `flags.bit1` + 32 bytes; expose a `compact_coinbase_or_none(coinbase)` decoder. (A)
- `digest.py:672-694` — when the coinbase is a compact record, source `_claimed` from `vm_engine.extract_state_root_compact(rec)` instead of `vm_engine.extract_state_root(_t[11])`; mandatory-root + mismatch logic unchanged (still 64-hex vs `node.vm_state_root`). (A)
- `miner.py:40-43`, `regnet.py:125-128` — when emitting a compact coinbase post-fork, write the raw-32 root + `flags.bit1` (and the hf2 readiness `flags.bit0`) instead of `embed_state_root(...)` / `FORK2_SIGNAL` ASCII; legacy path (`fork_height is None`) untouched. (A)
- `fork.py:29-31,154-165` — `has_fork_signal` reads `flags.bit0` from a compact coinbase; legacy substring scan retained for legacy coinbases. **REQUIRED.** (A)
- `rest_api.py:877`, `pool/optipoolware.py:109-112` — keep receiving the reconstructed 64-hex (`state_root_from_bytes` / `node.vm_state_root`), never raw bytes. (A)
- **(B) — does NOT ship in Stage-4; lands only with the re-sort:** `vm_state.py:45,58,69,92` (fold keys to `cid`, drop `":"`), `vm_state.py:43-49` (carry `addr` in the code value), `vm_state.py:97-119` (collect→reconstruct→**SORT**→hash, §B.2), plus the C8 sentinel + own-env wiring.

### E. Open-question dispositions

- **cid width** → 32-byte blake2b (C2), matching `block_store.pk`; uniform fixed-width LMDB prefix. Confirmed no caller assumes a 28-byte VM key prefix (the VM only reads `k[len(prefix):]`).
- **new root pre-image?** → **NO.** Keep the frozen `addr:word` pre-image AND require the §B.2 re-sort.
- **PoW nonce / openfield entropy** → the compact coinbase moves the nonce to its u64 `nonce` field (doc/29 §2.D); `check_block`'s PoW pre-image (`address + openfield + blockhash`, `miner.py:87`) must be reconstructed deterministically from the compact fields so the annealed hash stays bit-identical. This is a serialization-domain obligation; the VM root carrier is independent of it.
- **migration timing** → rides the existing startup/reorg rebuild path (`chain_ops.py:83,481`); no standalone tool; store is post-fork-only and empty on mainnet today.


**Savings**

| field | legacy form | legacy bytes | true-bytes form | new bytes | saving % |
|---|---|---|---|---|---|
| VM pre-state root (coinbase carrier) — **SHIPS (A)** | `"vmsr"` + 64-hex ASCII in openfield (`vm_engine.embed_state_root`, `vm_engine.py:155`) | 68 | 32 raw bytes (`flags.bit1` + `vm_state_root` field in compact coinbase) | 32 | 53% |
| VM root marker -> flag bit — **SHIPS (A)** | `"vmsr"` ASCII marker | 4 | 1 bit (shared `flags` byte, also carries hf2 readiness bit0) | ~0 | ~100% |
| storage slot value — already true bytes | word(32B) raw BE (`vm_state.py:71`) | 32 | word(32B) raw BE (unchanged) | 32 | 0% |
| balance value — already true bytes | bal(32B) raw BE (`vm_state.py:92`) | 32 | bal(32B) raw BE (unchanged) | 32 | 0% |
| code value — already true bytes | raw bytecode (`vm_state.py:45`) | N | raw bytecode (unchanged in (A); +57B addr header only if (B) lands) | N | 0% |
| storage slot key — **HELD BACK (B), gated on re-sort §B.2** | `addr(56-hex)` + `":"` + word(32B) (`vm_state.py:58,69`) | 89 | `cid(32B)` + word(32B) | 64 | 28% **(does NOT ship in Stage-4; CAVEAT: folding the on-disk key changes `state_root()` EMISSION ORDER, and the COMMITMENT is order-dependent — requires collect→reconstruct→SORT or it FORKS)** |
| code key — **HELD BACK (B)** | `addr(56-hex)` (`vm_state.py:45`) | 56 | `cid(32B)` (addr moved into value, +57B once/contract) | 32 | 43% **(same order-dependence caveat; ships only with §B.2 re-sort + §C test)** |
| balances key — **HELD BACK (B)** | `addr(56-hex)` (`vm_state.py:92`) | 56 | `cid(32B)` | 32 | 43% **(same order-dependence caveat)** |


**Adversarial fixes folded in:**
- REQUIRED FIX 'state_root() must collect-reconstruct-then-SORT': §B.2 replaces the in-loop-pre-image-only design with a full collect -> rebuild legacy full-key bytes (storage: addr.encode()+b':'+word; code/balances: addr.encode()) -> SORT by those legacy keys -> hash, so both the per-slot pre-image AND the emission SEQUENCE are byte-identical to the legacy on-disk order. Round-trip proof shows state_root(folded)==state_root(legacy) bit-for-bit including the cross-contract interleave. The design now states explicitly that reconstructing pre-image bytes alone is insufficient.
- REQUIRED FIX 'explicit characterization test before any flip': §C adds a BLOCKING test that compares state_root() over the cid-folded store against the frozen addr-prefixed layout byte-for-byte across (a) multiple contracts whose addresses interleave lexicographically and (b) a reorg/rebuild, and explicitly states the existing LMDB==sqlite-kv parity is INSUFFICIENT because both backends share the same (wrong) order — the test must compare folded vs frozen layouts.
- REQUIRED FIX 'add the iteration-order break to risks[] and savings caveats': risks[0] and risks[1] now state plainly that folding the on-disk key changes state_root() emission ORDER and the COMMITMENT is order-dependent (blake2b is sequential); the savings table marks every (B) row HELD BACK with the order-dependence caveat and that it ships only with the §B.2 re-sort + §C test.
- REQUIRED FIX 'resolve open_question #2 as NO new pre-image AND specify the re-sort': §B.2 and §E resolve it in writing — keep the frozen addr:word pre-image (no second root form, per the one-pre-image invariant) AND additionally require the re-sort, since freezing the pre-image is necessary but not sufficient without ordering fidelity.
- REQUIRED FIX 'fork.has_fork_signal must read flags.bit0, not the substring scan' promoted from open item to a REQUIRED fix: §A.3 specifies a form-aware has_fork_signal (compact record -> flags.bit0; legacy coinbase -> unchanged substring scan) and notes the readers fork.py:154-165 / dynamic_fork_height / lockin_at_tip use it; §D lists fork.py:29-31,154-165 as a required (A) change; a §C vector covers it.
- REQUIRED FIX 'if the simpler safer path is preferred, drop surface (B) and ship (A) alone': adopted as the Stage-4 DEFAULT — §0 and §B.0 state (B) does NOT ship by default; surface (A) (the raw-32 carrier) ships alone and is sound. (B) is fully specified but gated, shippable only with the §B.2 re-sort and §C test. The savings table and §D mark every (B) row as HELD BACK.
- Verified-GOOD items preserved: vm_engine.embed_state_root/extract_state_root left frozen, new siblings added beside (§A.2, §D); digest.py:680/691 string-equality of 64-hex means raw32.hex() reconstruction is exact (§A.2 round-trip proof); block_store.pk blake2b-32 precedent cited for the cid width (§B.1, C2); no A-hex regression — raw 32 bytes land in the LMDB block_store value, 64-hex reconstructed only for text consumers rest_api.py:877 / optipoolware.py:112 (§A.2, risks[5]).
- Added the C8 format-version sentinel risk for (B) (risks[7]) and the own-env / snapshot-rebuild discipline (§B.4, per C7) so an operator-copied pre-Stage-4 vm_state env force-rebuilds instead of silently missing cid-keyed lookups, and the prod 23 GB ledger is never hot full-scanned.


**Implementation checklist (residual review items to satisfy before wiring this domain):**
- [ ] Make the compact-coinbase PoW pre-image reconstruction LOSSLESS for the regnet path. Either: (a) add an explicit variable-length 'openfield_entropy'/rand field to the compact-coinbase record so the exact mined string ('hf2'+'vmsr'+root_hex+rand+nonce_hex) can be rebuilt byte-for-byte for check_block; OR (b) change regnet.py:128 to stop baking entropy into the vmsr field (mirror miner.py:43's embed_state_root(_sr,'') and carry all PoW entropy in the u64 nonce field only) so empty-rand reconstruction is exact -- and prove the nonce u64 covers regnet's needed search space. The chosen fix must be specified IN this VM domain spec (it owns embed_state_root's rand_hex), not deferred to the serialization domain as an open question.
- [ ] Add a sec C replay vector that mines a straddling REGNET chain through the compact-coinbase path and asserts mining_heavy3.check_block re-validates each post-fork block bit-identically after openfield/nonce reconstruction (replay_verify 0 mismatches). This is the test that currently would fail and must be made to pass before (A) ships.
- [ ] Specify exactly how the u64 nonce field round-trips to the miner's '%0x' % getrandbits(64) hex form: '%x' % nonce_u64 reproduces '%0x' (no leading zeros) so it is recoverable, but state this explicitly in sec A.2 and add it to the round-trip proof, since the PoW string depends on the precise hex rendering.

---

## 7. Address / recipient encoding (base58 -> raw)  `[address]`

## hf2 Stage-4 TRUE-BYTES storage — Domain: Address encoding (base58 → raw)

> Obeys the SHARED CROSS-DOMAIN CONVENTIONS verbatim. This domain touches **C5
> (address byte form policy)**, **C3 (txid forms are untouched here)**, **C6
> (Codec split — `block_store` values are msgpack, never JSON-routed)**, and **C8
> (format-version sentinel)**. Where this section and the shared section disagree,
> the shared section wins.

### 1. Scope and intent

Today the `address` and `recipient` fields of a stored tx are the **textual address
string** held inside the msgpack-packed block row (`block_store.py:114`, the `"t":
txs` list; after `r[1:]` drops `block_height`, address is the stored-tx field `t[1]`
and recipient is `t[2]`). For the Bismuth address families that `SignerFactory`
routes — secp256k1 `Bis1…`, ED25519 `Bis1…` (longer), native multisig
`Bism/mBis…`, ML-DSA-44/65/87 and secp256r1 (4-byte-versioned base58,
`signerfactory.py:96-117`), and legacy RSA 56-hex (`signerfactory.py:49`) — that
textual form is a base58check (or hex) re-encoding of a small fixed-width binary
payload. We store the **raw payload bytes** in the LMDB value (a genuinely binary
`bin8` element via `use_bin_type=True` — TRUE bytes, never hex-in-text, so it does
not trip the A-HEX regression), and reconstruct the exact original string on read.

**This is a STORAGE change only.** Per the shared governing invariants, the
consensus pre-image is untouched: `signature_buffer` / `signature_buffer_v2` /
`tx_id` / `tx_id_v2` / `_v2_tx_bytes` / `block_hash` / `block_hash_v2`
(`bismuth_serialize.py:23-147`) continue to take the **base58/hex address string**
exactly as before. `_expand` (`block_store.py:90-99`) rebuilds that string before
any row leaves the store (C-RECON), so every downstream consensus call, the REST
API, and `storage_backend.cross_check` see byte-identical strings.

**C5 compliance (address byte form is all-or-nothing across address-keyed stores).**
This domain moves address **values inside `block_store` rows** to raw bytes. It does
**not** by itself change the address *keys* of `balance_index.bal` /
`token_index` (`addrtok`/`cred`/`deb`/`alias_*`) / `vm_state` / `shieldedv1`. Those
keyed stores are governed by C5's all-or-nothing rule and are handled in their own
domain sections under the same raw-byte address form; this section is the
`block_store`-row half of that coordinated migration and **must not** be shipped as
a lone raw-byte address representation that disagrees with the keyed stores. The
recovered/dispatched address handed to `signerfactory` is always the reconstructed
base58/hex **string** (C5: storage-raw, dispatch-string — the regex path at
`signerfactory.py:120-134` is never fed raw bytes).

### 2. The hard constraint: the field is NOT always an address

`address`/`recipient` are free-form strings, **truncated to 56 chars** at write time
(`digest.py:261-262`: `db_address = str(transaction[1])[:56]`,
`db_recipient = str(transaction[2])[:56]`). On the real chain they include
non-address sentinels that MUST survive byte-for-byte:

- `"genesis"` — the genesis sender (`genesis.py:74,93`). Does **not** match the RSA
  regex; takes the verbatim branch.
- `SHIELD_SINK = sha224(b"bismuth-shield-pool").hexdigest()` (`shieldedv1.py:42`) —
  a 56-lowercase-hex sink that **matches `RE_RSA_ADDRESS`** (`signerfactory.py:49`,
  `^[abcdef0-9]{56}$`). It is correctly and losslessly captured by tag `0x00`
  (`bytes.fromhex(s).hex() == s` verified). `vm_custody` and similar 56-hex sinks
  behave identically.
- VM contract addresses, `"Hypernode"`, and any operation-driven recipient string
  that is not a valid address family — verbatim branch.

base58 is **not** a safe universal codec here: arbitrary text containing `0 O I l`
(e.g. `"0xABCDEF"`, `"foo_bar"`) is not base58-decodable at all, and even decodable
text is only losslessly reversible when it is the **canonical** encoding of its
decoded bytes. So the design is a **tagged union** with a verbatim UTF-8 fallback.
The tag is the first byte of the blob; it selects the reconstruction rule. The
**verbatim 0xFF fallback is load-bearing for correctness, not a size knob** — a
56-char-truncated mid-address, or any non-canonical text, MUST fail the encoder's
round-trip guard and land in 0xFF.

### 3. Byte layout

The stored value for an address/recipient field is a binary blob occupying the
msgpack value element where today a string sits. With `use_bin_type=True` the Codec
writes it as `bin8` (a 2-byte prefix for bodies < 256 bytes), so the on-disk cost is
`2 + blob_len`. The blob is `tag(u8) || body`.

```
addr_blob = tag(u8) || body

tag    meaning                  body
0x00   RSA / 56-hex / 56-hex sink   = bytes.fromhex(addr_str)        (raw digest bytes)
0x01   secp256k1  (Bis1…, short)    = base58.b58decode(addr_str)     (full decoded byte string)
0x02   ED25519    (Bis1…, long)     = base58.b58decode(addr_str)     (full decoded byte string)
0x03   ML-DSA / secp256r1 (versioned) = base58.b58decode(addr_str)   (full decoded byte string)
0x04   native multisig (Bism/mBis)  = base58.b58decode(addr_str)     (full decoded byte string)
0xFF   verbatim fallback            = len(u8) || raw UTF-8 bytes     (length-prefixed)
```

**Tags 0x01–0x04 are NOT fixed-width and the version bytes do NOT determine the
width.** The `RE_ECDSA_ADDRESS` regex (`signerfactory.py:51`) permits a variable
base58 tail (`Bis1` + `{28,52}`), and `RE_MULTISIG_ADDRESS` (`signerfactory.py:54`)
permits `{28,56}`, so the `b58decode` output is **arbitrary length**. The body is
stored as the **whole `b58decode(addr_str)` output**, and reconstruction is exactly
`base58.b58encode(blob[1:]).decode()` with **no stored length** — the blob length
minus 1 is the body length, which is all `b58encode` needs. This preserves leading
`1` characters (leading zero bytes) because the *entire* decoded byte string,
including leading zeros, is what is stored (verified:
`b58encode(b58decode("112VfUX")) == "112VfUX"`).

Illustrative observed widths (NOT relied upon by the codec):

```
tag 0x00  RSA / 56-hex / sink:  1 + 28  = 29   (28 = len(bytes.fromhex(56hex)))
tag 0x01  secp256k1 Bis1 (~37c): 1 + ~27 = ~28
tag 0x02  ED25519  Bis1 (~54c):  1 + ~40 = ~41
tag 0x03  ML-DSA/secp256r1(~54c):1 + ~40 = ~41
tag 0x04  multisig Bism (~37c):  1 + ~27 = ~28
tag 0xFF  verbatim (≤56 chars):  1 + 1 + len   (len ≤ 56, always fits the u8)
```

### 4. Encode rule (write path) — total and self-checking

A pure module-level function `pack_addr(s: str) -> bytes` (new in `block_store.py`,
lazy-importing `base58` and `SignerFactory` to keep the RSA-only path light, per
`signerfactory.py:40-46`). It is applied to the stored-tx fields `t[1]` (address)
and `t[2]` (recipient) inside `BlockStore.put_blocks` (`block_store.py:110-113`),
right where the row is shrunk before `txn.put(self.blocks, …)`, alongside the
existing `t[self._PK] = self._pubkey_id(...)`:

```
def pack_addr(s):
    s = str(s)
    # 0xFF u8 length bound — REQUIRED, locks the fallback to a single byte (open_question #4)
    b = s.encode()
    # 1) RSA / 56-hex / 56-hex sink: the strict ^[abcdef0-9]{56}$ predicate the verifier uses.
    if SignerFactory.address_is_rsa(s):                 # signerfactory.py:172-174
        raw = bytes.fromhex(s)
        if raw.hex() == s:                              # canonical lowercase-hex round-trip guard
            return b"\x00" + raw
        return _verbatim(b)                             # non-canonical hex (shouldn't happen) -> verbatim
    # 2) base58 families — classify, then PROVE the round-trip before committing to a fixed tag.
    try:
        signer = SignerFactory.address_to_signer(s)     # signerfactory.py:120-134
        import base58
        body = base58.b58decode(s)
        if base58.b58encode(body).decode() != s:        # canonical-encoding guard
            raise ValueError                            # non-canonical -> verbatim
        tag = _TAG_FOR_SIGNER(signer)                   # ECDSA->0x01, ED25519->0x02, versioned->0x03, multisig->0x04
        return bytes([tag]) + body
    except Exception:
        pass
    # 3) genesis / sinks / contract / any non-address / truncated mid-string -> verbatim.
    return _verbatim(b)

def _verbatim(b):
    assert len(b) <= 255, "address/recipient utf-8 exceeds u8 length bound"   # REQUIRED bound
    return b"\xff" + bytes([len(b)]) + b
```

Tag selection from the classified signer class:
- `SignerECDSA` (`is_single_sig_ecdsa`, `Bis1` len ≤ 50) → `0x01`
- `SignerED25519` (`is_single_sig_ed25519`, `Bis1` len > 50) → `0x02`
- `SignerMLDSA44/65/87` / `SignerSECP256R1` (versioned, `_versioned_signer`) → `0x03`
- `SignerMultisig` (`Bism/mBis`) → `0x04`

The **classify → b58decode → b58encode round-trip guard** is what makes the encoder
**total and self-checking**: any string that does not *provably* re-encode to itself
(non-canonical base58, unexpected family, a 56-char-truncated address, raw text)
falls through to the verbatim branch. Correctness never depends on base58 being a
universal codec. The `len(b) <= 255` assert is **mandatory** (it cannot fire on
real data because the field is `[:56]`-truncated, but it locks the u8 format
invariant rather than leaving it implicit).

**JSON-fallback codec guard (C6 / REQUIRED).** `pack_addr` introduces the **first
`bytes` value** into the `{"h": …, "t": [...]}` msgpack map (today that map holds
only `str`/`int` — the pubkey field is an integer dedup id). `Codec.pack` falls back
to `json.dumps` when `msgpack` is unavailable (`kvstore.py:59-64`), and `json.dumps`
**raises `TypeError` on a `bytes` value** (verified). To stop a silent break on an
msgpack-less deployment, `BlockStore.__init__` (or the first `put_blocks` that would
write a packed address) **asserts the active codec is msgpack**:

```
# block_store.py __init__ (or guarding put_blocks):
assert Codec.backend == "msgpack", \
    "address true-bytes storage requires the msgpack codec (kvstore.py:58); " \
    "the JSON fallback cannot serialize the bin address blob"
```

This is per C6: `block_store` values are the **msgpack** column, never JSON-routed.
The guard makes the dependency loud at open time instead of a `TypeError` mid-block.

### 5. Reconstruction rule (read path) — lossless, with a mandatory transition guard

A pure inverse `unpack_addr(value) -> str` (new in `block_store.py`), applied in
`BlockStore._expand` (`block_store.py:90-99`, beside the existing pubkey-id
re-expansion) to rebuild stored-tx `t[1]` and `t[2]`:

```
def unpack_addr(value):
    # REQUIRED transition guard: a plain str is an old-format (pre-Stage-4) or
    # mid-rollback row — return it unchanged. use_bin_type=True + raw=False make
    # str vs bytes reliably distinguishable on unpack (kvstore.py:52,56).
    if isinstance(value, str):
        return value
    tag = value[0]; body = value[1:]
    if tag == 0x00:
        return body.hex()                          # 28 raw bytes -> 56 lowercase hex
    if 0x01 <= tag <= 0x04:
        import base58
        return base58.b58encode(body).decode()     # full b58decode output -> exact string
    if tag == 0xFF:
        n = body[0]
        return body[1:1 + n].decode()              # verbatim
    raise ValueError("unknown address tag 0x%02x" % tag)
```

**The `isinstance(value, str)` guard is a HARD requirement of `unpack_addr`, not a
risk note.** During any rolling deploy, the `blocks` db can hold a mix of old
string-valued rows and new blob-valued rows; the guard makes the mixed store read
back losslessly. (`use_bin_type=True` writes str as msgpack `str` and bytes as `bin`,
and `raw=False` on unpack returns them as Python `str` vs `bytes` respectively — so
the two are unambiguously distinguishable, `kvstore.py:52,56`.)

**Losslessness (round-trip proof) — `unpack_addr(pack_addr(s)) == s` for every `s`
the field can hold:**
- **0x00**: `bytes.fromhex(s)` then `.hex()` round-trips because RSA addresses and
  56-hex sinks are canonical **lowercase** hex (Python `.hex()` is lowercase; the
  field is `^[abcdef0-9]{56}$`-validated; the encoder also asserts `raw.hex() == s`).
- **0x01–0x04**: `body == base58.b58decode(s)` and the encoder asserted
  `base58.b58encode(body).decode() == s`, so `b58encode(body)` reproduces `s`
  byte-for-byte; leading-`1`/leading-zero bytes are preserved because the **whole**
  decoded byte string is stored (verified empirically).
- **0xFF**: stores `s.encode()` verbatim and slices `body[1:1+body[0]]` back —
  trivially exact for any UTF-8 string ≤ 255 bytes (real fields are ≤ 56 chars).

Therefore `_expand` yields rows **byte-identical** to today's. The frozen consensus
pre-images consume `_expand` output (the string), so signature buffers, txid, and
block hash are unaffected; `storage_backend.cross_check` (SQLite vs LMDB) and
`block_store.verify_against_sqlite` stay byte-for-byte; `replay_verify` stays
0-mismatch (see §8).

### 6. Fork gate (by destination height) and pre-fork byte-identity

**No new gate and no second signal** — fully consistent with the one-fork-signal
invariant. The encoding lives entirely inside `BlockStore`, and the block store is
opened/written **only post-fork**: `storage_backend.select(node)` returns
`LmdbBackend`/`LmdbWriteBackend` only when
`fork_height is not None and last_block >= fork_height` and a `block_store` is
present (`storage_backend.py:152-158`); the write path runs
`LmdbWriteBackend.append_block` → `put_block` → `put_blocks`
(`storage_backend.py:202-203`). Every block that reaches `put_blocks` is therefore a
**destination-height ≥ `fork_height`** block by construction (gate by destination
height, never a global mode).

With `fork_height is None` (mainnet today and **all** historical blocks),
`select` returns `SqliteBackend` (`storage_backend.py:158`), the block store is never
written or read, and the legacy SQLite TEXT address columns are the sole, untouched
form — so **pre-fork on-disk bytes cannot change** and there is **no historical block
in the block store to re-serialize**. The legacy LMDB on-disk region (if a node
already has a block_store from a prior phase) keeps its current encoding; the new
blob form applies only to the v2 region. `amounts.LEDGER_INTEGER` is a storage flag,
orthogonal to this gate, and does not participate.

**Format-version sentinel (C8 / dispatch site).** `block_store` carries
`meta[b"fmt"]` (Stage-4 = `b"\x04"`). On open under Stage-4 code, if the sentinel is
**absent or lower**, the store **refuses to serve consensus reads until rebuilt**
(it is the canonical store, not a droppable projection — per C8) and an operator
rebuild re-writes it from a snapshot copy (C7), never a hot full-scan of the 23 GB
prod ledger. This is what stops an operator-copied pre-Stage-4 env (string address
values) from opening cleanly and being read by code that expects blob values. The
sentinel is written atomically with the rebuild's first commit. (Note: the
`isinstance(str)` guard in §5 already makes mixed rows safe to *read*; the sentinel
makes a wholesale-old env safe to *reject* loudly.)

### 7. Code that changes (file:line)

- `block_store.py:110-113` — `put_blocks`: after `t = list(r[1:])`, add
  `t[1] = pack_addr(t[1]); t[2] = pack_addr(t[2])` (alongside
  `t[self._PK] = self._pubkey_id(...)`).
- `block_store.py:90-99` — `_expand`: add `t[1] = unpack_addr(t[1]);
  t[2] = unpack_addr(t[2])` while rebuilding the row (the `isinstance(str)` guard in
  `unpack_addr` makes an old string value a no-op).
- `block_store.py:52-56` — `__init__`: add `assert Codec.backend == "msgpack"` guard
  (the JSON-fallback regression fix) and the `meta[b"fmt"]` Stage-4 sentinel
  check/write (C8). (`meta` is added to the `dbs=[…]` open list.)
- `block_store.py` (new module-level) — `pack_addr` / `unpack_addr` / `_verbatim` /
  tag table, with lazy `base58` + `SignerFactory` imports.
- **No change** to `bismuth_serialize.py`, `digest.py`'s write tuple
  (`digest.py:297-305`), or any consensus pre-image. No DB column/schema change — the
  `blocks` value element for those two fields simply becomes a tagged `bin` blob
  instead of a `str`.

### 8. Characterization / replay test (REQUIRED — proves 0-mismatch)

A new characterization test (re-baselined per C6/C8, since this is a deliberate
fork-gated `str→bin` format break on the `block_store` value) MUST:

1. **Exercise every tag** on representative real fields, including the full set of
   non-address recipients seen on chain: `"genesis"` (0xFF), `SHIELD_SINK` (0x00,
   56-hex sink), `vm_custody`/contract addresses (0xFF), `"Hypernode"` (0xFF), plus
   a real `Bis1…` secp256k1 (0x01), a long `Bis1…` ED25519 (0x02), a versioned
   ML-DSA/secp256r1 (0x03), and a `Bism/mBis…` multisig (0x04). Assert
   `unpack_addr(pack_addr(s)) == s` for each, including a leading-`1` base58 case and
   a non-canonical/`0OIl`-containing string (must take 0xFF).
2. Run `replay_verify` at **`fork_height=None`** (100% legacy path — block_store
   never touched, must be 0-mismatch) **and** across a **straddling pre+post-fork
   regnet chain** (must be 0-mismatch through `_expand`).
3. Run `storage_backend.cross_check` (`storage_backend.py:130`) and
   `block_store.verify_against_sqlite` over the straddling chain and assert
   **byte-for-byte** equality on the reconstructed rows.
4. Assert `Codec.backend == "msgpack"` is enforced (the JSON-fallback guard) and
   that an env without `meta[b"fmt"] == b"\x04"` refuses consensus reads (C8).


**Savings**

| field | legacy form | legacy bytes | true-bytes form | new bytes | saving % |
|---|---|---|---|---|---|
| address — secp256k1 `Bis1…` (~37-char) | msgpack `str` of base58 (37 chars) | 39 | msgpack `bin8` of `0x01 ‖ ~27 raw` | 30 | ~23% |
| recipient — secp256k1 `Bis1…` (~37-char) | msgpack `str` of base58 (37 chars) | 39 | msgpack `bin8` of `0x01 ‖ ~27 raw` | 30 | ~23% |
| address — ED25519 `Bis1…` (~54-char) | msgpack `str` of base58 (54 chars) | 56 | msgpack `bin8` of `0x02 ‖ ~40 raw` | 43 | ~23% |
| address — ML-DSA / secp256r1 (~54-char) | msgpack `str` of base58 (54 chars) | 56 | msgpack `bin8` of `0x03 ‖ ~40 raw` | 43 | ~23% |
| address — native multisig `Bism…` (~37-char) | msgpack `str` of base58 (37 chars) | 39 | msgpack `bin8` of `0x04 ‖ ~27 raw` | 30 | ~23% |
| address/recipient — RSA or 56-hex sink (`SHIELD_SINK`) | msgpack `str` of 56-hex (56 chars) | 58 | msgpack `bin8` of `0x00 ‖ 28 raw` | 31 | ~47% |
| recipient — verbatim e.g. `"genesis"` (7 chars) | msgpack `str` (7 chars) | 8 | msgpack `bin8` of `0xFF ‖ len ‖ 7` | 13 | −63% (rare sentinel; absorbed) |
| recipient — verbatim `"Hypernode"` (9 chars) | msgpack `str` (9 chars) | 10 | msgpack `bin8` of `0xFF ‖ len ‖ 9` | 13 | −30% (rare sentinel; absorbed) |

> Byte counts verified empirically: msgpack `str` of N chars (N<32) = 1+N; `bin8` of M bytes (M<256) = 2+M. The `bin8` 2-byte prefix is why the rare verbatim sentinels grow by ~5 bytes — acceptable because genesis/Hypernode/contract-sink recipients are a negligible fraction of rows, while the dominant secp256k1/RSA cases save 23–47%. Tags 0x01–0x04 widths are illustrative (variable-length b58 tail); the codec stores the full b58decode and never relies on a per-family fixed width.


**Adversarial fixes folded in:**
- REQUIRED FIX 1 (JSON-fallback codec crash): Added an explicit `assert Codec.backend == 'msgpack'` guard in BlockStore.__init__ (block_store.py:52-56) — verified at kvstore.py:58/70 that Codec.backend is the discriminating class attribute and confirmed empirically that json.dumps raises TypeError on a bytes value embedded in the {'h','t'} map. The bin address blob can therefore never reach the JSON-fallback path silently; the dependency fails loudly at open. Documented under §4 (JSON-fallback codec guard) and as a code-change line in §7, consistent with C6 (block_store values are the msgpack column, never JSON-routed).
- REQUIRED FIX 2 (_expand type-guard promoted from risk to hard requirement): unpack_addr now BEGINS with `if isinstance(value, str): return value` as a non-optional first step (§5), justified by use_bin_type=True + raw=False making str vs bytes reliably distinguishable on unpack (verified at kvstore.py:52,56). Stated explicitly that this is a HARD requirement for mixed-form/transition stores during rolling deploy, not a risk note.
- REQUIRED FIX 3 (mandatory u8 length bound in 0xFF branch): _verbatim() now contains `assert len(b) <= 255` as a mandatory invariant (§4), locking open_question #4. Cross-checked digest.py:261-262 that db_address/db_recipient are [:56]-truncated, so the assert can never fire on real data but locks the format.
- REQUIRED FIX 4 (characterization/replay test for every tag incl. 0xFF non-address recipients): Added §8 specifying a test that exercises genesis (0xFF), SHIELD_SINK (0x00 — confirmed at shieldedv1.py:42 it matches RE_RSA_ADDRESS and round-trips losslessly), vm_custody/contract addresses (0xFF), Hypernode (0xFF), plus 0x01-0x04 families incl. a leading-`1` base58 case and a 0OIl non-canonical string; runs replay_verify at fork_height=None AND a straddling chain, plus storage_backend.cross_check and verify_against_sqlite for byte-for-byte 0-mismatch through _expand. Re-baseline framed as the sanctioned C6 fork-gated format break.
- REQUIRED FIX 5 (section-3 wording — do not claim 0x01-0x04 fixed-width): §3 now states explicitly that tags 0x01-0x04 are NOT fixed-width and the version bytes do NOT determine the width (RE_ECDSA_ADDRESS {28,52} and RE_MULTISIG_ADDRESS {28,56} at signerfactory.py:51,54 permit variable base58 tails). The body is the full b58decode output of arbitrary length and reconstruction is base58.b58encode(blob[1:]).decode() with NO stored length. Per-family width rows are explicitly labeled illustrative-only, and the savings table footnote repeats this.
- ADDITIONAL HARDENING (C5 all-or-nothing address policy): Added explicit §1/§5 framing that this is the block_store-value half of the coordinated raw-byte address migration and must land together with the address-KEYED stores (balance_index/token_index/vm_state/shieldedv1), and that dispatch always reconstructs the base58/hex string before signerfactory (storage-raw, dispatch-string). Also added the C8 meta[b'fmt'] Stage-4 sentinel (§6/§7) so an operator-copied pre-Stage-4 env refuses consensus reads rather than silently mis-reading string values as blobs.
- Corrected the savings table: legacy/new bytes verified empirically (msgpack str of N<32 chars = 1+N; bin8 of M<256 bytes = 2+M). Fixed the verbatim-sentinel saving figures (genesis 8->13 = -63%, added Hypernode 10->13 = -30%) which the original under-counted, and added a footnote explaining the bin8 2-byte prefix overhead on rare short sentinels. Index correction noted in §1: after r[1:] the address is stored-tx field t[1] and recipient t[2] (confirmed against block_store.py:110-113 and the _PK=5 comment at block_store.py:48-50).


**Implementation checklist (residual review items to satisfy before wiring this domain):**
- [ ] Correct the fork-gating analysis to match the WIRED write path. The block_store write is gated on node.block_writer (set when config.block_store is on, node.py:1715-1722) and runs for EVERY digested block at digest.py:734, NOT on fork_height. Either (a) add an explicit `block_instance.block_height_new >= node.fork_height` guard around the append_block call (and around build_from_sqlite's start height) so pack_addr genuinely only sees v2 rows -- making the section-6 claim true -- or (b) drop the false 'post-fork-only by construction' framing and re-justify the encoding as safe for the ENTIRE block_store regardless of fork height (which the lossless round-trip actually supports), and remove the 'v2 region only / legacy region untouched' language for this store.
- [ ] Reconcile the C8 fmt-sentinel + 'v2 region only' story with whichever gating choice is made. If the encoding applies to all heights (option b), the sentinel governs the whole env and there is no mixed legacy-string vs v2-blob region to protect; if it is truly fork-gated (option a), specify how a single env holds legacy-string rows below fork and blob rows at/above it and how _expand's isinstance(str) guard distinguishes them height-by-height.
- [ ] Make pack_addr classification CANONICAL and deterministic independent of installed optional packages, OR explicitly drop the C7 raw-byte state-root claim for the block_store. Either classify by a dependency-free method (regex/version-prefix inspection that does not import the optional signer, mirroring _versioned_signer's b58decode-then-version-bytes but WITHOUT loading the signer class), so the same address always yields the same tag/body on every node; or state that the block_store env's on-disk bytes are intentionally non-canonical and its self-verification must run over RECONSTRUCTED rows (as cross_check/verify_against_sqlite already do), not a raw-byte env hash.
- [ ] Make base58 a hard dependency on the READ path wherever a block_store can hold tag 0x01-0x04 rows (already listed as a risk -- promote to required): unpack_addr lazily imports base58 and will ImportError on read of any base58-family address if the runtime lacks it. Fail loudly at block_store open (alongside the Codec.backend=='msgpack' assert) on any node that may serve such rows.

---

## 8. Core side-indexes (txid / balance / hashes / pk-pkr)  `[core-indexes]`

## Stage-4 TRUE-BYTES Core Side-Indexes (txid_index, balance_index, hashes, pk/pkr)

> Obeys the SHARED CROSS-DOMAIN CONVENTIONS. Where this section touches a shared
> encoding, the convention wins: height keys per **C1**, blake2b widths per **C2**,
> the single canonical txid byte form per **C3**, amount units per **C4**, address
> raw-byte policy per **C5**, the Codec split per **C6**, per-env layout + the
> per-projection migration flip per **C7**, and the **C8** format-version sentinel.

### 0. Status, scope, and the one invariant that frees this section

These four stores are **LOCAL, rebuildable projections**, NOT consensus pre-images.
Nothing here enters `signature_buffer`/`tx_id`/`block_hash` or any v2 sibling
(`bismuth_serialize.py:23-55`, `:77-147`) — those frozen functions are untouched
(C0 "pre-fork byte-identity is frozen"). The authority is the legacy SQLite ledger
(pre-fork) / `block_store` (post-fork). Each store is drop-and-rebuilt at boot and
on every reorg path: `txid_index.rebuild_from_cursor` (`txid_index.py:81-98`),
`balance_index.rebuild_from_cursor` (`balance_index.py:113-131`),
`block_store.put_blocks`/`rollback` (`block_store.py:102-130`).

The only correctness obligation is **internal consistency + lossless reconstruction
of the form the reader expects** (a 64-hex txid string round-trip, a
`credit - debit` int, an int height, a public-key column string). The on-disk byte
form can change freely **as long as every read API returns the byte-identical
Python objects it returns today** — which is what makes these the cleanest
TRUE-BYTES wins in the codebase.

**The A-HEX REGRESSION to kill (C0 "A-hex regression ban"):** today
`txid_index._key` (`txid_index.py:61-63`) and `block_store` `hashes` keys via `_bh`
(`block_store.py:67-69`) store a hash as its **64-/56-char hex string `.encode()`d
to UTF-8** — 2 bytes on disk per digest byte. These are *LMDB key byte strings*, so
we store the genuine 28/32 **raw** bytes (1:1). This is a binary store, not the
rejected A-hex wire encoding.

**Per-env layout (C7).** `txid_index` and `balance_index` are each **their own
projection env**; `hashes`/`pk`/`pkr` are sub-dbs **inside the canonical
`block_store` env**. A projection drop+rebuild therefore never fragments the
canonical store. Per **C1 HARD INVARIANT**, `block_store` holds only
positive-real-height keys and shares no env with `reward_chain`'s negated keyspace.

---

### 1. txid_index (txid -> height)

**Today (`txid_index.py:61-63`, `:77`):** key = `txid.encode()` where `txid` is the
64-char lowercase hex from `tx_id_at` => **64 UTF-8 bytes** for a 32-byte digest
(2×). Value = `h.to_bytes(8, "big")` => 8 raw bytes (already TRUE bytes, kept).

**TRUE-BYTES key — the raw 32-byte blake2b-256 (C2: `digest_size=32`) content txid
(C3: the single canonical post-fork txid byte form):**

```
txid_index "txid" KEY (raw, fixed)
field        | type                       | width
-------------|----------------------------|-------
txid_digest  | blake2b-256 raw bytes      | 32     <- bytes.fromhex(tx_id_at(...))
                                                     total 32 B
```

```
txid_index "txid" VALUE (raw, fixed)  -- UNCHANGED
field   | type           | width
--------|----------------|-------
height  | u64 BIG-endian | 8       <- h.to_bytes(8,"big"); C1 height form; BE so it
                                     stays scan-ordered for rollback's value-scan
                                     total 8 B
```

The value stays **big-endian** by deliberate exception: `rollback()`
(`txid_index.py:106`) value-scans with `int.from_bytes(v, "big")`. This is the C1
height form, so it is already correct and unchanged.

**Change site:** `txid_index.py:61-63` `_key()` becomes
`bytes.fromhex(txid) if isinstance(txid, str) else txid` (accept a 64-hex str OR
raw 32 bytes; output the 32 raw bytes). Every producer flows through
`txid_of`/`_txid_fields` (`txid_index.py:35-50`) which return the hex string from
`tx_id_at`, so **only `_key` changes**; `apply_rows` (`:77`),
`rebuild_from_cursor` (`:94-96`), `height_of`/`contains` (`:112-124`) all route
through `_key` unchanged.

**Reconstruction rule (C-RECON, round-trip proof):** `bytes.fromhex` ∘ `.hex()` is
the identity on lowercase even-length hex, so `_key(txid_hex).hex() == txid_hex` for
every 64-hex input. There is no consensus form to rebuild — the txid string is
recomputed on demand from the ledger row via `txid_of`/`tx_id_at` and never read
back out of this index. The only externally observable value (the height int from
`height_of`) is byte-identical to today. **Per C3 this raw-32 form is the dedup
identity only for `height >= fork_height`**; pre-fork dedup remains the legacy
`signature[:56]` slice (C3 "legacy dedup boundary"), and `txid_index` is empty
pre-fork (see §5), so a cross-boundary dedup comparison must compare the two forms
across the fork, never assume one spans both.

---

### 2. balance_index (address -> balance)

**Today (`balance_index.py:34`, `:87`):** value = `Codec.pack([credit_units,
debit_units])` — a **msgpack** 2-element list of ints (per C6 `balance_index` is a
msgpack-valued store, NOT a JSON-parity store). A large mainnet balance packs as
`0x92` (fixarray-2) + two uint64-tagged ints = 1 + 9 + 9 = **19 bytes**. Key = raw
address bytes (`balance_index.py:45-46`) — already the consensus address string as
held by the ledger; **kept verbatim per C5** (the address-keyed migration is
all-or-nothing across all address-keyed stores and is OUT OF SCOPE here; do not
shrink it — that would break parity vs `ledger_balance3` and the cross-store
address identity).

**TRUE-BYTES value — exactly ONE encoding: two fixed-width 16-byte little-endian
unsigned 128-bit counters** (resolving the rejected "ships both" ambiguity — the
varint alternative is REMOVED):

```
balance_index "bal" VALUE (raw, fixed) -- THE ONE chosen encoding
field        | type            | width
-------------|-----------------|-------
credit_units | u128 LITTLE-end | 16      <- (c).to_bytes(16,"little")
debit_units  | u128 LITTLE-end | 16      <- (d).to_bytes(16,"little")
                                          total 32 B
```

Per **C4**: BIS amounts are *bounded* (capped supply ⇒ u64 would suffice
arithmetically), but credit/debit are **cumulative running sums** over the full
ledger (including the negative-height reward mirrors folded in), so u128 gives
ceiling-free headroom on hot exchange addresses at a fixed +16 B for a one-row-per-
address index. (This is a BIS balance, not a token amount, so the C4 token-overflow
rule does not bind it; u128 is chosen for headroom, not necessity.)

**NEGATIVE-INTERMEDIATE HAZARD — RESOLVED (was a reorg crash).** `(n).to_bytes(16,
"little")` raises `OverflowError` on any negative `n`, whereas the old
`Codec.pack` tolerated negatives. The exposed `rollback_rows` API
(`balance_index.py:94-96` → `_apply(..., -1)` → `_pack([c + sign*dc, d + sign*dd])`
at `:87`) could in principle drive a transient negative before the matching apply.
The fix is a **non-negativity guard in the pack step**, making the running-total
invariant explicit and the encoding total:

- Replace the two `Codec` aliases (`balance_index.py:34-35`) with a local pair:
  ```python
  def _pack(c, d):
      if c < 0 or d < 0:
          raise ValueError("balance_index running totals must stay non-negative: c=%d d=%d" % (c, d))
      return c.to_bytes(16, "little") + d.to_bytes(16, "little")
  def _unpack(v):
      return int.from_bytes(v[:16], "little"), int.from_bytes(v[16:], "little")
  ```
  The explicit `ValueError` (a) documents the invariant the wired digest path
  already upholds — `digest.py:834` drives `apply_rows`, and rebuilds go through
  `rebuild_from_cursor` which only writes **final non-negative dict totals**
  (`balance_index.py:127-131`) — and (b) converts the latent silent `OverflowError`
  into a loud, named guard if a future caller ever rolls back out of apply order.
  Credit and debit are independently monotonic non-decreasing under correct
  apply/rollback pairing, so this guard never fires on the wired path. (An
  alternative — signed two's-complement i128 — was rejected: it doubles the decode
  branch and masks the genuine ordering bug this guard surfaces.)

**Change sites:** `balance_index.py:34-35` (the `_pack`/`_unpack` aliases) plus the
three call sites that already route through them — `_get` (`:80` `_unpack(v)`),
`_apply` (`:87` `_pack([...])` → `_pack(c+sign*dc, d+sign*dd)`), and
`rebuild_from_cursor` (`:130` `_pack([c, d])` → `_pack(c, d)`). No other change.

**Reconstruction rule (C-RECON, round-trip proof):** for any `(c, d)` with
`0 <= c, d < 2^128`, `_unpack(_pack(c, d)) == (c, d)` exactly (fixed-width int
round-trips are bijective). The only read API, `get_balance_units` /
`get_balance` (`:134-141`), returns `c - d` — the identical int it returns today —
so the bit-match parity against `ledger_balance3` (integer mode, exact
order-independent integer addition, `balance_index.py:13-17`) holds at the
**int/semantic** level, which is what the parity test asserts. Per **C4 C-RECON**,
any path that round-trips these units back into a consensus pre-image MUST go
through `amounts.consensus_amount` (= `from_units`) — never `display_amount`; this
index never forms a pre-image, but the discipline is noted so a future caller does
not regress it.

---

### 3. hashes db (block_hash -> height)

**Today (`block_store.py:67-69`, `:115`):** key = `block_hash.encode()` where
`block_hash` is the hex string the ledger holds — **56 UTF-8 bytes** pre-fork
(sha224, 28 bytes) and **64 UTF-8 bytes** post-fork (blake2b-256 C2 width 32) — i.e.
2× the raw digest. Value = `_hk(height)` = `struct.pack(">Q", height)` = 8 raw BE
bytes (C1 height form, already optimal).

**TRUE-BYTES key — the raw digest.** The two destination-height regimes have
different digest widths, self-describing by length (28 vs 32); no tag is needed
because the reader always supplies a hex string of the matching width:

```
hashes KEY (raw, variable by fork regime)
field   | type      | width
--------|-----------|-----------------------------------------
digest  | raw bytes | 28 (pre-fork sha224) | 32 (post-fork blake2b-256, C2)
                      total 28 or 32 B
```

```
hashes VALUE (raw, fixed) -- UNCHANGED
field  | type           | width
-------|----------------|-------
height | u64 BIG-endian | 8       <- Codec.hkey(height) == struct.pack(">Q",height)
                                    C1 height form; total 8 B
```

**Change site:** `block_store.py:67-69` `_bh()` becomes
`bytes.fromhex(block_hash) if isinstance(block_hash, str) else block_hash`.
`_bh` is the **single choke point**: used by `put_blocks` (`:115`), `rollback`
(`:126`), and `height_by_hash` (`:166`), so all three move to raw keys together.

**The `blocks`-db value `"h"` field stays the hex STRING** (`block_store.py:114`,
read at `:139,:162`): `get_block`/`block_hash`/`storage_backend.cross_check`
(`storage_backend.py:139`) return it as the hex string the SQLite row holds, a
byte-parity obligation against the reference backend (C0 "replay-validated",
`verify_against_sqlite` `block_store.py:238-255`). Per **C6** `block_store` values
are msgpack and `"h"` is text inside that value — only the `hashes` *index key* (a
pure local reverse-lookup) becomes raw. This is the C6-sanctioned scope: the
JSON/text value stays, only the raw-key choke point flips.

**Reconstruction rule (C-RECON, round-trip proof):** `hashes` is a reverse index
only — `height_by_hash` (`:164-167`) takes a hash, returns a height, never
reconstructs the hash. For any hex `h`, `_bh(h).hex() == h` (lowercase-hex
identity), so a `put` then `height_by_hash(h)` resolves the same raw key and returns
the byte-identical 8-byte height. The forward block-hash string lives untouched in
the `blocks` value and is returned verbatim. `cross_check`
(`storage_backend.py:130-142`) and `verify_against_sqlite` (`block_store.py:238-255`)
compare `get_block`/`block_hash` outputs and rows — none read the raw `hashes` key
bytes — so both stay byte-for-byte green.

---

### 4. pk / pkr dedup tables (public-key store-once) — already TRUE bytes, FROZEN

**Today (`block_store.py:79-86`):** `pk` key = `blake2b(pkb, digest_size=32)
.digest()` = **32 raw bytes** (C2: the `block_store` pubkey-dedup 32-byte width);
value = `_hk(id)` = 8 raw BE bytes (C1). `pkr` key = `_hk(id)` = 8 raw BE bytes (C1);
value = `pkb` = the raw public-key bytes (~1068 B RSA, 33 B ecdsa). **Already fully
TRUE-BYTES; no hex/text anywhere. No change.** Locked here against future
regression:

```
pk KEY (raw, fixed)                       pk VALUE (raw, fixed)
field   | type             | width         field | type           | width
--------|------------------|-------         ------|----------------|-------
pk_hash | blake2b-256 (32) | 32             id    | u64 BIG-endian | 8 (C1)
          total 32 B                              total 8 B

pkr KEY (raw, fixed)                       pkr VALUE (raw, variable)
field | type           | width             field  | type      | width
------|----------------|-------            -------|-----------|---------------------
id    | u64 BIG-endian | 8 (C1)            pubkey | raw bytes | len (~1068 RSA / 33 ecdsa)
        total 8 B                                   total = value length
```

**FREEZE (C5 + byte-parity):** `pkr`'s value is the **raw bytes of the public_key
COLUMN STRING** the ledger holds (base64 text for RSA/ML-DSA), i.e. `pk.encode()` of
the wire field — NOT a re-decode to DER. `_expand` (`block_store.py:90-99`) does
`pkb.decode()` to restore the exact column string; `verify_against_sqlite`
(`:250`) asserts `get_block == SQLite row`. Decoding to DER would break that
byte-parity. The genuine raw-byte wire pubkey/pubkey-by-reference compaction is the
**separate hf2 §2.C / Stage-3 signers work, OUT OF SCOPE** here.

**Reconstruction rule (C-RECON):** `_expand` reads `pkr[_hk(id)]` and `.decode()`s
it to the original column string; `_pubkey_id` (`:71-88`) is the inverse, assigning
`next id = txn.count(pk)` within the write txn. `_expand(put_blocks(rows)) == rows`
for the public_key column, proven live by `verify_against_sqlite` (`:250`). Lossless
because the stored value is exactly the input string's UTF-8 bytes; the blake2b key
is only a dedup probe (any collision is caught by `verify_against_sqlite`).

---

### 5. Fork gate (by DESTINATION height) and pre-fork byte-identity (C0)

**One gate, by destination block height, reading `node.fork_height`** — no second
signal, no global mode (C0).

- **txid_index** is already destination-height-gated at the row level: `apply_rows`
  returns 0 when `fork_height is None` (`txid_index.py:69-70`) and skips any row with
  `h <= 0 or h < int(fork_height)` (`:75`); `rebuild_from_cursor` indexes only
  `WHERE block_height >= fork_height` (`:91-93`) and returns 0 when
  `fork_height is None` (`:86`). With **mainnet `fork_height = None` the store is
  EMPTY by construction** — 100% legacy path — so the raw-key change is unreachable
  pre-fork and pre-fork is byte-identical trivially (no entries). Post-fork the txid
  is the C3 canonical content-txid (`tx_id_v2_s` via `tx_id_at`, `:41`); its raw 32
  bytes are the natural key.

- **hashes** and **pk/pkr** live in the `block_store` env, selected only post-fork
  (`storage_backend.select` gates on `fork_height` set AND `last_block >=
  fork_height` AND store present, `storage_backend.py:145-158`). The `hashes` raw-key
  width tracks the **block's own hash regime** via the single `_bh` choke point: a
  loaded pre-fork block carries 56-hex sha224 → 28 raw bytes; a post-fork block
  carries 64-hex blake2b → 32 raw bytes. The width is a pure function of the block's
  hash string, **never a mode flag**, so any historical block re-serializes to the
  byte-identical raw key regardless of node config. The legacy `SqliteBackend`
  (`storage_backend.py:53-102`) never opens these indexes, so the frozen pre-fork
  branch is the untouched SQLite reads (C0 "legacy on-disk byte-identity").

- **balance_index** is intentionally NOT consensus-fork-gated (`balance_index.py:101-
  103`, `:116-118`): it scans the ENTIRE ledger including the negative-height reward
  mirrors to bit-match `ledger_balance3`. Per **C0** it depends ONLY on
  `amounts.LEDGER_INTEGER` (the STORAGE flag, decoupled from `fork_height`,
  `balance_index.py:13-17`) — never on the consensus gate. The value-form swap
  applies uniformly to every rebuilt entry and changes nothing observable (same
  `c - d` int).

All four are **drop-and-rebuilt** on reorg/boot, so an upgraded node rebuilds once;
there is no in-place migration of mixed-format rows.

---

### 6. Format-version sentinel (C8) — makes the hex→raw flip self-healing

**Was a STALE-FILE SILENT MISS; now resolved per C8.** An operator who copies an OLD
hex-keyed env onto NEW raw-keyed code would otherwise open cleanly and **silently
miss every lookup** (the new code probes raw-byte keys that don't exist in the old
hex layout). Fix: each of these envs carries a **1-byte format-version sentinel in a
`meta` sub-db**, exactly as `token_index` (`token_index.py:58,92`) and `shieldedv1`
(`shieldedv1.py:347,354`) already do.

- Add `"meta"` to the `dbs=` list at `open_store` time:
  `txid_index.py:55` (`dbs=["txid"]` → `["txid","meta"]`),
  `balance_index.py:70` (`dbs=["bal"]` → `["bal","meta"]`),
  `block_store.py:55` (`dbs=["blocks","hashes","pk","pkr"]` → add `"meta"`).
- On first open under Stage-4 code, read `meta[b"fmt"]`:
  - **absent or `< b"\x04"`** ⇒ the env predates the raw-byte layout. **Projections**
    (`txid_index`, `balance_index`) `txn.drop` every data db and **force a full
    rebuild** from the ledger/snapshot (cheap, already the boot path); the
    **canonical** `block_store` **refuses consensus reads until rebuilt** (it is not
    drop-cheap — rebuild from a snapshot copy per C7, never a hot full-scan of the
    23 GB prod ledger).
  - `meta[b"fmt"] = b"\x04"` is written **atomically with the rebuild's first
    commit** (C8).
  - present and `== b"\x04"` ⇒ the env is already raw-keyed; open normally.

This converts the silent correctness hole into a loud, self-healing rebuild and
satisfies C8 for all three new-format envs (`block_store` already shares one env, so
one sentinel covers `hashes`/`pk`/`pkr`).

---

### 7. Characterization-test re-baseline (MANDATORY — these tests pin the LEGACY bytes)

Per **C6/C7**, every JSON/codec→binary or hex→raw flip is a deliberate, fork-gated
format break with its **own characterization re-baseline**. Two existing
on-disk-byte locks pin the OLD format and MUST be re-pinned in the SAME change that
flips the encoding (otherwise they fail):

1. **`tests/test_balance_index.py:96-133`
   (`test_lmdb_on_disk_bytes_identical_to_direct_lmdb`).** It builds `expected` via
   `_orig_pack([c, d])` = **msgpack** bytes (`:104-105,:128-132`) and asserts
   `on_disk == expected` (`:133`). Switching the value to two-u128-LE makes
   `on_disk != expected`. **Re-pin** the expected dict to the new TRUE-BYTES form:
   ```python
   def _be(c, d):  # the new raw value: two u128 little-endian
       return c.to_bytes(16, "little") + d.to_bytes(16, "little")
   expected = {
       b"A": _be(40, 100 + 10),
       b"B": _be(100, 0),
       b"C": _be(0, 40 + 5),
   }
   ```
   Drop the `_orig_pack`/msgpack scaffold (`:101-110`). The parity test
   `test_rebuild_bitmatches_sql_synthetic` (`:60-77`) is indifferent — it compares
   `get_balance_units` ints, not bytes — and stays green.

2. **`tests/test_block_store.py:30-32,148-186`
   (`test_on_disk_bytes_identical_to_direct_lmdb`).** The `_block` fixture uses
   `bh = "hash%08d" % h` (`:31`) — a **NON-hex** synthetic block_hash. The new
   `_bh = bytes.fromhex(block_hash)` raises `ValueError("non-hexadecimal number
   found")` on `b"hash%08d"`. (Real ledger hashes are valid hex, so production is
   fine; only this fixture is invalid.) **Re-pin** the fixture to a valid-hex
   block_hash and update the raw-key assertion:
   ```python
   def _block(h, ntx=2):
       bh = "%064x" % h          # 64-hex (valid hexadecimal), distinct per height
       return h, bh, [_row(h, i, bh) for i in range(ntx)]
   ```
   and at the on-disk assert (`:178-179`) probe the **raw** key instead of the hex
   string:
   ```python
   assert rec["h"] == "%064x" % 1                         # blocks-value "h" stays hex (C6)
   assert txn.get(bytes.fromhex("%064x" % 1), db=hashes) == k1   # hashes key is RAW now
   ```
   The corrupt-block fixture in `test_build_and_verify_against_sqlite`
   (`block_store.py` test `:207`, `s.put_block(10, "hash00000010", ...)`) must
   likewise use a valid-hex hash (e.g. `"%064x" % 10`). The forward-value `"h"` field
   stays the hex string (C6, §3), so only the reverse-index key assertion moves.

These re-baselines are the C7-sanctioned format-break locks, not silent edits.

---

### 8. Replay / cross-check obligations (C0 "replay-validated", C7 migration flip)

- `replay_verify` MUST stay **0-mismatch at `fork_height=None`** (txid_index empty;
  block_store env not opened) **AND 0 across a straddling pre+post-fork regnet chain**
  (post-fork raw-key txids resolve identically; the read values — heights, balances,
  pubkey strings — are byte-identical to today).
- `storage_backend.cross_check` (`storage_backend.py:130-142`) and
  `block_store.verify_against_sqlite` (`block_store.py:238-255`) compare reconstructed
  12-field rows and the `blocks`-db hex hash string; none read the raw
  `hashes`/`pk`-hash key bytes, and `pkr`/`_expand` still returns identical column
  strings — both remain byte-for-byte green.
- **Migration flip is per-projection (C7).** `txid_index_consensus` and the
  balance-read flip each go to primary ONLY after that one projection runs in
  shadow + `parity_strict` (txid_index vs the legacy signature-scan dedup;
  balance_index vs `ledger_balance3`) and `replay_verify` reports 0 mismatches over a
  straddling chain. Never all-at-once.


**Savings**

| field | legacy form | legacy bytes | true-bytes form | new bytes | saving % |
|---|---|---|---|---|---|
| txid_index key (txid) | 64-char hex digest `.encode()` UTF-8 | 64 | raw blake2b-256 digest (C2 width 32, C3 canonical txid) | 32 | 50% |
| txid_index value (height) | `to_bytes(8,"big")` (already raw, C1) | 8 | u64 big-endian (unchanged) | 8 | 0% |
| balance_index value (credit,debit) | msgpack `[c,d]` (fixarray + 2 tagged uint64) | 19 | two u128 LE fixed (the ONE chosen encoding; varint dropped) | 32 | -68% (headroom; one row per address, +13 B fixed) |
| balance_index key (address) | consensus address string (C5, kept verbatim) | 56 | unchanged (C5 all-or-nothing, out of scope) | 56 | 0% |
| hashes key (pre-fork) | 56-char hex sha224 `.encode()` UTF-8 | 56 | raw sha224 digest (28) | 28 | 50% |
| hashes key (post-fork) | 64-char hex blake2b `.encode()` UTF-8 | 64 | raw blake2b-256 digest (C2 width 32) | 32 | 50% |
| hashes value (height) | `Codec.hkey` BE u64 (already raw, C1) | 8 | u64 big-endian (unchanged) | 8 | 0% |
| blocks-value "h" (forward hash) | hex string in msgpack (C6, byte-parity) | n/a | unchanged (kept hex; out of scope) | n/a | 0% |
| pk key (pk_hash) | blake2b-256 raw (C2 width 32, already raw) | 32 | unchanged | 32 | 0% |
| pk value (id) | BE u64 raw (already raw, C1) | 8 | unchanged | 8 | 0% |
| pkr key (id) | BE u64 raw (already raw, C1) | 8 | unchanged | 8 | 0% |
| pkr value (pubkey, RSA) | raw column-string bytes (already raw, FROZEN per C5) | ~1068 | unchanged (frozen; DER decode forbidden) | ~1068 | 0% |
| meta[b"fmt"] sentinel (C8, NEW) | (did not exist) | 0 | 1-byte version `b"\x04"` per env | 1 | n/a (+1 B/env, prevents silent miss) |


**Adversarial fixes folded in:**
- REQUIRED FIX 1 (re-pin both legacy-byte characterization tests): Section 7 explicitly re-pins tests/test_balance_index.py:96-133 — replace the msgpack `_orig_pack([c,d])` expected bytes with `_be(c,d)=c.to_bytes(16,'little')+d.to_bytes(16,'little')` for keys A/B/C — and tests/test_block_store.py:30-32,178-179 — change the `_block` fixture from the non-hex `bh='hash%08d'` to valid-hex `bh='%064x'%h` so `bytes.fromhex` does not raise, probe the hashes db with `bytes.fromhex('%064x'%1)` instead of the hex string, and fix the corrupt-block fixture at :207 to valid-hex. Both are labelled C7-sanctioned format-break re-baselines done in the SAME change as the encoding flip.
- REQUIRED FIX 2 (resolve negative-intermediate hazard): Section 2 replaces the Codec aliases with a local `_pack` that raises a named `ValueError` when credit or debit is negative (instead of the silent OverflowError from `to_bytes(...,'little')`), documenting the running-total non-negativity invariant the wired digest.py:834 apply path + rebuild_from_cursor (final-totals-only) already uphold. Chose the non-negative-guard route over signed two's-complement (explicitly rejected: it doubles the decode branch and masks the ordering bug). This is the chosen resolution of original open-question 5.
- REQUIRED FIX 3 (pick exactly ONE balance encoding): Section 2 ships ONLY the two-u128-LE fixed 32-byte form and REMOVES the varint-pair alternative entirely (it is no longer in the section or the savings table). The savings table now lists a single balance_index value row.
- REQUIRED FIX 4 (self-healing hex->raw migration): Section 6 adds the C8 1-byte format-version sentinel meta[b'fmt']=b'\x04' to each env — adding 'meta' to the open_store dbs= list at txid_index.py:55, balance_index.py:70, block_store.py:55 (precedent: token_index.py:58,92 and shieldedv1.py:347,354 already carry a meta db). On first open, absent/lower version forces drop+rebuild for projections and refuses-consensus-until-rebuilt for the canonical block_store (rebuilt from a snapshot copy, never a hot prod full-scan); the sentinel is written atomically with the rebuild's first commit. This closes the operator-copied-old-env silent-miss hole.
- ADVERSARIAL reorg_safe=False resolved: the two reorg-path regressions — balance_index OverflowError on a transient negative during rollback_rows, and the stale-file silent miss after a copied env — are both fixed (fix 2 guard + fix 4 sentinel), so the drop-and-rebuild-on-reorg path is now crash-free and self-healing.
- Bonus consistency with SHARED CONVENTIONS: every blake2b cites its C2 digest_size (32 for txid/pk/block-hash); every height field cites C1; the txid raw-32 dedup boundary cites C3 incl. the legacy signature[:56] cross-boundary caveat; the balance address key is held verbatim per C5 (all-or-nothing, out of scope); the block_store 'h' value stays hex/msgpack per C6 with only the reverse-index key flipped to raw; per-env layout and the per-projection migration flip cite C7; amounts cite C4 incl. the consensus_amount/display_amount C-RECON discipline. domain returned exactly as 'core-indexes'. No A-hex anywhere — all 'raw' fields are true bytes in LMDB keys/values.


**Implementation checklist (residual review items to satisfy before wiring this domain):**
- [ ] Make block_store._bh SAFE on untrusted input on the LOOKUP path. `bytes.fromhex(block_hash)` must not raise on malformed input reaching height_by_hash from rest_api.py:1182 and apihandler_blocks.py:53. Either (a) make height_by_hash catch the ValueError and return None (preserving today's graceful-miss -> SQLite-fallback -> clean 404), e.g. `try: key=self._bh(block_hash) except ValueError: return None`; OR (b) validate the hash at both API boundaries (raise _BadRequest / clean reject) before calling height_by_hash. Note _bh is also used on the WRITE path (put_blocks:115, rollback:126) where input is trusted ledger hex — keep that strict; only the read/lookup entry must tolerate junk. Add a regression test feeding a non-hex and an odd-length block_hash to both height_by_hash and the REST /block-by-hash + socket BLOCK handler, asserting a clean miss/404, not a 500/exception.
- [ ] Add to section 7 / the test re-baseline: a characterization test that block_store.height_by_hash(<non-hex>) and height_by_hash(<odd-length-hex>) return None rather than raising, locking the graceful-miss contract that the hex->raw key flip must preserve.
- [ ] Update the design's open-question #2 (and the corresponding risk) to cover VALIDITY of untrusted block_hash input at the API/socket boundary, not just fork-mismatched WIDTH — the width audit alone does not catch the bytes.fromhex(ValueError) regression.

---

## 9. Plugin stores (tokens / aliases / shielded / reward / fee)  `[plugin-stores]`

## hf2 Stage-4 TRUE-BYTES LMDB storage — Plugin / derived stores (tokens / aliases / shielded / reward / fee)

> Obeys the SHARED CROSS-DOMAIN CONVENTIONS verbatim. Where this section and that section disagree, the shared section wins. The conventions touched here: **C0** (one fork signal, gate-by-destination-height, none-means-legacy, A-hex ban, legacy byte-identity, replay-validation, no hot full-scan), **C1** (`Codec.hkey` 8B BE height; the `reward_chain` negated-keyspace / separate-env invariant), **C2** (every `blake2b` cites `digest_size`), **C3** (canonical txid raw-32 — with the **explicit carve-out below**), **C4** (atomic units; **bounded BIS = u64, unbounded TOKEN = varint/u128**; `consensus_amount` reconstruction discipline), **C5** (address all-or-nothing raw migration; dispatch keeps base58), **C6** (Codec split — these stores keep frozen-JSON / decimal / sentinel today; any flip is a fork-gated re-baseline), **C7** (per-env layout, per-env state-root, snapshots, per-projection migration flip), **C8** (1-byte `meta[b"fmt"]` sentinel).

### 0. What this domain is, and what it is NOT

These stores are **derived, rebuildable projections of already-validated blocks** — `token_index` (tokens v2 + aliases v2, `token_index.py`), `shieldedv1.ShieldedState` (`shieldedv1.py`), `reward_chain` (`reward_chain.py`), and the computed-not-stored `fee_dynamics` signal. **None is a consensus byte form** the way `signature_buffer`/`tx_id`/`block_hash` are; none touches the frozen functions in `bismuth_serialize.py`; there is no wire re-serialization. Per **C7** each lives in its **own projection env**, and the SQLite→LMDB consensus-read flip is **per-projection** (shadow + `parity_strict` until `replay_verify` shows 0 mismatches over a straddling chain).

| store | role | consensus-bearing? | why |
|---|---|---|---|
| `shieldedv1.kimg` key-image set | double-spend / nullifier gate | **YES (indirectly)** | `validate_block` rejects a re-used key image via `state.has_key_image` (`shieldedv1.py:378-380,768,793`). A wrong/aliased key = a double-spend or a false reject. Held to the same reconstruction rigor as a consensus pre-image. |
| `shieldedv1.notes` (`P`/`R`/`C`) | ring membership / outputs | **YES (indirectly)** | `_resolve_ring`/`_resolve_ring_v3` read `p_pub`/`commitment` from `state.note()` (`shieldedv1.py:365-372,524-562,626`). A lossy note rejects a block that should validate. |
| `shieldedv1.flows` / `meta.pool` | pool accounting | informational (audit) | pool total is an invariant *check* vs `ledger_balance(SHIELD_SINK)`, not a per-tx reject. |
| `token_index` (all 11 dbs) | token balances / aliases | informational | tokens are **consensus-inert** (`plugins/tokens_aliases/__init__.py:14-15`); a wrong sum only mis-accepts a *token* transfer, never a block hash. |
| `reward_chain` | dev/HN reward mirror | informational | balance-preserving sidecar; never in a block body or hash (`reward_chain.py:13-16`). |
| `fee_dynamics` | next base fee | **derived input (consensus)** | recomputed each block from `block_store.recent_block_weights` (`digest.py:582-594`); **no store of its own** (§6). |

---

### 1. The single transform: drop hex/decimal-text, store TRUE bytes in the LMDB value/key

Per **C0** A-hex ban, every win here is one rule applied per field, into an **LMDB value or key byte string** (never hex-in-text):

- 32-byte hash stored as 64 ASCII hex → **32 raw bytes**.
- 33-byte compressed secp256k1 point stored as 66 ASCII hex → **33 raw bytes**.
- integer stored as ASCII-decimal → **fixed-width LE** (BIS, bounded) or **LEB128 varint** (token, unbounded — see **C4** and §3).
- JSON object → **packed binary record** (LE, length-prefixed) — NOT msgpack-of-the-same-strings, NOT hex.

Heights already are TRUE bytes (`Codec.hkey` `>Q` BE, **C1**) and **stay byte-for-byte**; the height-ordered secondary keys (`notes_h`, `kimg_h`, `flows`, `cred`/`deb` `_party_key` tail, `alias_rev`, `journal`/`ajournal`) keep their exact byte layout so range-delete rollback (`shieldedv1.py:450-479`, `token_index.py:332-355,453-482`) is untouched.

---

### 2. shieldedv1 — exact TRUE-BYTES layouts (the consensus-bearing one)

**Identifier forms.** `note_id = blake2b? no` — it is `sha256` domain-hash `.hex()` → **32 raw bytes** (`note_id`, `shieldedv1.py:144-145`). Per **C3** legacy boundary: this `note_id` is the shielded store's own identity and is **not** the `tx_id_v2` content-txid; it stays a 32-byte digest in raw form. Points `P`/`R`/`C`/key-image are **33-byte compressed** secp256k1 points (`.format()`).

**`notes` value — packed binary record** (replaces JSON at `shieldedv1.py:431`; reconstructs the exact dict `note()` returns at `shieldedv1.py:371-372`):

```
field      | type             | width      | source
-----------|------------------|------------|------------------------------------------
ver        | u8               | 1          | 1 = transparent v1/v2, 3 = RingCT
height     | u64 LE           | 8          | d["h"]  (BIS-side height, bounded)
amt        | u64 LE           | 8          | d["amt"] atomic units (0 for v3 hidden) — BIS, BOUNDED -> u64 OK (C4)
tok_len    | u8               | 1          | len(tok) <= 64 (consensus cap shieldedv1.py:577,610)
tok        | utf-8            | tok_len    | d["tok"]
R          | bytes            | 33         | compressed point bytes.fromhex(d["R"])
P          | bytes            | 33         | compressed point bytes.fromhex(d["P"])
C          | bytes            | 33         | commitment / Pedersen point bytes.fromhex(d["C"])
memo_len   | u32 LE           | 4          | RAW ciphertext byte count (see memo note)
memo       | bytes            | memo_len   | RAW AES-GCM blob nonce(16)||tag(16)||ct  (NOT base64)
```

**`notes` key:** 32 raw bytes (`note_id` digest) — was 64 hex.
**`notes_h` key:** `height(8B BE) || note_id(32B)` — was `8B BE || 64-hex-as-text`.
**`kimg` key:** 33 raw compressed-point bytes — was 66 hex. **`kimg` value:** `height u64 BE` (unchanged, **C1**).
**`kimg_h` key:** `height(8B BE) || image(33B)`.
**`flows` value:** `i64 LE` signed delta (was decimal string). **`flows` key** unchanged (`height 8B BE || seq 8B BE`).
**`meta` values:** `nnotes`/`nki`/`flowseq` = `u64 LE`; `pool` = **`i64 LE`** (it is transiently signed mid-rollback — `pool -= delta` at `shieldedv1.py:475` can dip negative before re-add) — were decimal strings.

**Memo width (fix — confirmed against source).** Two distinct consensus caps exist: transparent notes cap memo at **4096** (`_require_note_fields`, `shieldedv1.py:612`), RingCT v3 notes cap at **65536** (`_require_confidential_note`, `shieldedv1.py:579`). The cap is on the **base64 *string* length** the validator sees. A `u16` prefix (max 65535) would truncate a 65536-char v3 memo. The store holds the **raw decoded ciphertext** (`b64decode` of `_encrypt_memo`'s canonical output, `shieldedv1.py:157,163-164`), whose max byte count is `floor(65536/4)*3 = 49152` < 65535 — but we use a **`u32 LE` (4-byte) prefix** to make truncation structurally impossible and to leave headroom if the v3 cap is ever raised; the 2 extra bytes vs u16 are negligible against the base64→raw saving. **Round-trip:** on read, `b64encode(nonce||tag||ct).decode()` reproduces the EXACT canonical base64 string the validator/reader consumed (`_decrypt_memo` slices `blob[:16]/[16:32]/[32:]`, `shieldedv1.py:164`); `_encrypt_memo` only ever emits canonical, unpadded-issue-free base64, so `b64encode(b64decode(s)) == s` for every validated note. The packed record stores the raw `nonce||tag||ct`; the empty-memo case (`""`) stores `memo_len=0`.

**Key-image canonicalization (HARD, fix).** Before computing any raw `kimg`/`kimg_h` key, the 33-byte compressed form MUST be produced exactly as the verifier does: `image33 = cc.PublicKey(bytes.fromhex(raw)).format()` (the canonicalization at `shieldedv1.py:625`), then `assert len(image33) == 33`. A 65-byte uncompressed point MUST be rejected/canonicalized **before** the key is taken — otherwise the same image submitted uncompressed vs compressed becomes two distinct keys and slips past `has_key_image`, re-enabling a double-spend (the exact bug the comment at `shieldedv1.py:622-624` warns of). The pack step refuses to store a non-33-byte key.

**Reconstruction (C-RECON, round-trip proof).** `note()` must return byte-identical fields to the JSON path: `r_pub`/`p_pub`/`commitment`/`R`/`P`/`C` are re-rendered as **lowercase hex** via `raw.hex()` (Python `bytes.hex()` is always lowercase, matching `.format().hex()` and `note["R"]`); `amount`/`create_height` are the identical Python ints; `token` the identical str; `memo` the identical base64 str. The ring verifier does `bytes.fromhex(nt["p_pub"])` (`shieldedv1.py:626`) and re-derives `note_id`/commitment — so re-rendering hex on read keeps the **ring-signature pre-image (`_ring_message`, `shieldedv1.py:277-281`) byte-identical**. `unpack(pack(d)) == d` for every field any reader observes (`{h,tok,amt,R,P,memo,C}` plus `ver`).

---

### 3. token_index — exact TRUE-BYTES layouts

**Token amounts use a LEB128 unsigned varint, NOT u64 (fix — confirmed defect).** Token supply/cred/deb/journal.amt are **arbitrary unbounded ints**: `supply = int(supply)` with no `SATOSHIS` cap (`token_index.py:164`), `_sum_party`/`_group_sums` accumulate `int(v)` (`token_index.py:147,288`), `apply_transfer` does `amount = int(amount)` (`token_index.py:189`). A single supply or a cumulative cred/deb sum exceeding 2⁶⁴−1 would silently truncate under fixed u64, diverging `token_balance`/`_group_sums`/`token_detail` from the SQLite `SUM`/`GROUP BY` cross-check (**C7**). Per **C4** (bounded vs unbounded width), **every token amount field is a self-sizing LEB128 unsigned varint** (lossless, no upper bound). (u128 is the alternative but caps; varint is chosen so it can never overflow.) `u64 LE` is used **only** for BIS-denominated, supply-bounded fields (shielded `amt`/`height`, reward `amount`).

**cred / deb value** = `varint(amount)` (was `str(amount).encode()`, `token_index.py:176-177,196-197`). **Keys unchanged** (`token \0 party \0 H(8B BE) Q(8B BE)` via `_party_key`, `token_index.py:70-73`) so `_sum_party` prefix scans (`token_index.py:138-148`) and `tokens_rollback` `_party_key` deletes (`token_index.py:344-345`) are untouched.

**addrtok / tokset value** = `varint(count)` (was decimal string, `token_index.py:223,230`) — counts are bounded but varint keeps the integer-field encoding uniform and avoids a u32 cap on tokset. **meta** (`seq`, `tok_anchor`, `alias_anchor`) = `varint` (were decimal strings, `token_index.py:113,118`).

**tokreg value — packed record** (replaces JSON at `token_index.py:172-173`):

```
field    | type            | width      | source
---------|-----------------|------------|------------------------------------------
height   | u64 LE          | 8          | "h"  (BIS-side height, bounded)
ts_len   | u8              | 1          | len of the VERBATIM ts string
ts       | utf-8           | ts_len     | "ts" stored EXACTLY as fed (tx[_TS]) — NOT quantized (fix)
iss_len  | u8              | 1          | len(issuer)
issuer   | utf-8           | iss_len    | "issuer"
supply   | varint          | var        | "supply" — TOKEN amount, UNBOUNDED -> varint (C4)
txid_len | u8              | 1          | len of the txid TEXT
txid     | utf-8           | txid_len   | "txid" stored as TEXT (see txid carve-out)
```

**`ts` is stored verbatim as a length-prefixed UTF-8 string, not centiseconds (fix).** The plugin feeds the raw block-body timestamp `tx[_TS]` (`plugins/tokens_aliases/__init__.py:138,158`); the journal `ts` reader returns it unchanged as `timestamp` (`token_index.py:275`). A `round(ts*100)` centisecond round would diverge if the value is ever not exactly 2-dp. Storing the exact string guarantees byte-identity for both `tokreg.ts` and `journal.ts` regardless of ts format. (`tokreg.ts` has no current reader, but the same rule is reused for `journal.ts` which IS read, so the discipline is uniform.)

**txid carve-out (fix — reconciles with C3).** **C3 makes the raw-32 `tx_id_v2` content-txid the dedup identity at/above the fork.** But `token_index` does **not** key by the content-txid: the plugin feeds `str(signature)[:56]` (a legacy signature-slice, possibly non-hex text) or, when that is `"0"`, `_blake2b_txid(...)` = `blake2b(..., digest_size=20).hexdigest()` = 40-hex text (`plugins/tokens_aliases/__init__.py:28-29,138,145,149`). This feed is **never raw bytes and never the consensus content-txid.** Therefore `seen` keys and `tokreg.txid`/`journal.txid` stay **length-prefixed UTF-8 text**, exactly as fed. Forcing a hex re-decode here is the A-hex trap in reverse (non-hex signature prefixes throw; 56-char sig-slice ≠ 64-hex). The C3 raw-32 dedup applies to `txid_index` (a different store), not to this plugin's `seen`/`tokreg.txid`. (Moving the plugin feed to raw content-txid is a separate, fork-gated change with its own re-baseline per **C6**; until then the saving for these two fields is the JSON-framing removal only, not a hex→raw halving.)

**journal value — packed record, kind-tagged (fix: carry `ts`; reconstruct exact dict).** Three kinds are written: issue `{k,tok,rcp,adr,amt,txid}` (`token_index.py:181-182`), transfer `{k,tok,rcp,adr,amt,txid,ts}` (`token_index.py:201-202` — **note the `ts`**), noop `{k,txid}` (`token_index.py:215`).

```
field    | type   | width    | notes
---------|--------|----------|----------------------------------------------------
kind     | u8     | 1        | 0=issue  1=transfer  2=noop
-- if kind==0 (issue): --
 tok     | u8 lp + utf-8     | "tok"
 rcp     | u8 lp + utf-8     | "rcp"
 adr     | u8 lp + utf-8     | "adr"
 amt     | varint            | "amt"  (TOKEN amount, unbounded -> varint)
 txid    | u8 lp + utf-8     | "txid" (text, per carve-out)
-- if kind==1 (transfer): --
 tok     | u8 lp + utf-8     | "tok"
 rcp     | u8 lp + utf-8     | "rcp"
 adr     | u8 lp + utf-8     | "adr"
 amt     | varint            | "amt"
 txid    | u8 lp + utf-8     | "txid"
 ts      | u16 lp + utf-8    | "ts"  -- REQUIRED so token_txs_for_address (token_index.py:275) returns it (fix)
-- if kind==2 (noop): --
 txid    | u8 lp + utf-8     | "txid"
```

**Kind-tag ↔ dict reconstruction (fix — must satisfy `op["k"]` at `token_index.py:341,342`).** `unpack` rebuilds the EXACT legacy dict for each kind, including the `"k"` value (`"issue"`/`"transfer"`/`"noop"`), and for transfer including `"ts"`. `tokens_rollback` reads `op["k"]` then `op["tok"]/["rcp"]/["adr"]/["txid"]` (`token_index.py:341-353`) — all present after unpack. `token_txs_for_address` reads `j.get("k")`, `j.get("tok"/"rcp"/"adr"/"amt"/"txid"/"ts")` (`token_index.py:272-277`) — `ts` now present. Round-trip: `unpack(pack(j)) == j` for all three kinds (issue/noop have no `ts` key and the unpacked dict also has none).

**alias_fwd value — packed record** (replaces `{"a":addr,"h":N}` JSON, `token_index.py:370,393`):

```
field    | type            | width      | source
---------|-----------------|------------|---------------------------
addr_len | u8              | 1          | len(address)
address  | utf-8           | addr_len   | "a"
height   | u64 LE          | 8          | "h"  (bounded -> u64)
```

`alias_owner`/`addfromalias`/`transfer_alias`/`free_alias`/`aliases_rollback` read `json.loads(cur)["a"]` and `["h"]` (`token_index.py:380,388,391,410,468,473,475,478`); unpack returns the identical `{"a":str,"h":int}` dict. **`alias_rev`** is already a sentinel `b""` with a binary `address \0 H(8B BE) \0 alias` key (`token_index.py:371`) — **unchanged**.

**ajournal value — per-kind packed record (fix — three distinct shapes; integer `prev_h`; free has NO `a`).** Confirmed shapes: register writes `{al, a}` with **no `k`** (`token_index.py:372`); transfer writes `{k:"transfer", al, a, prev, prev_h}` (`token_index.py:396-397`); free writes `{k:"free", al, prev, prev_h}` with **no `a`** (`token_index.py:414-415`). `prev_h` is read as an **int** (`token_index.py:473,478`).

```
field    | type            | width      | notes
---------|-----------------|------------|----------------------------------------------------
kind     | u8              | 1          | 0=register  1=transfer  2=free
 al      | u16 lp + utf-8              | "al" (alias; present in all three)
-- if kind==0 (register): --
 a       | u16 lp + utf-8             | "a"  (address)   -- NO k, NO prev/prev_h
-- if kind==1 (transfer): --
 a       | u16 lp + utf-8             | "a"  (new owner)
 prev    | u16 lp + utf-8             | "prev" (prior owner)
 prev_h  | u64 LE                     | "prev_h" (INTEGER, bounded height)
-- if kind==2 (free): --
 prev    | u16 lp + utf-8             | "prev" -- NO a (free carries no address)
 prev_h  | u64 LE                     | "prev_h" (INTEGER)
```

**Kind-tag ↔ dict reconstruction for ajournal (fix — satisfy `op.get("k","register")` at `token_index.py:462`).** `aliases_rollback` does `kind = op.get("k","register")` (`token_index.py:462`), then for register reads `op["a"]` (`:465`), for transfer reads `op["a"]/["prev"]/int(op["prev_h"])` (`:473`), for free reads `op["prev"]/int(op["prev_h"])` (`:478`) and **never** `op["a"]`. `unpack` therefore rebuilds:
- kind 0 → `{"al":..,"a":..}` (no `"k"` key) so `op.get("k","register")=="register"` resolves exactly.
- kind 1 → `{"k":"transfer","al":..,"a":..,"prev":..,"prev_h":<int>}`.
- kind 2 → `{"k":"free","al":..,"prev":..,"prev_h":<int>}` with **no `"a"` key**.

This makes a register→transfer→free unwind (replayed newest-first, `token_index.py:460`) restore the exact prior `alias_fwd`/`alias_rev` state: free-undo re-puts `{a:prev,h:prev_h}` and the rev key; transfer-undo deletes the new-owner rev key and re-puts the prior owner; register-undo drops `alias_fwd` only if it still holds height `h` and deletes the register rev key. `unpack(pack(op)) == op` for all three (register's unpacked dict has no `"k"`/`"prev"`/`"prev_h"`; free's has no `"a"`) — i.e. byte-identical to what `aliases_rollback` reads.

---

### 4. reward_chain — exact TRUE-BYTES layout

**mirror_hash is blake2b-20 (40-hex / 20 raw bytes), NOT sha224-28 (fix — original design was wrong).** `calculate_mirror_hash` is `blake2b(..., digest_size=20).hexdigest()` (`digest.py:898`, **C2** width pinned at 20 for this use) → a 40-char lowercase hex string. Legacy SQLite mirror rows lifted by `extract_from_ledger` (`reward_chain.py:96-98`, the `r[7]` column) may carry the older sha224 (56-hex / 28B) form for very old heights. Because the form is **variable**, the mirror hash is stored **length-prefixed**, never fixed-28. Today the value is msgpack list-of-`[sender, recipient, amount_int, mirror_hash_str]` (`reward_chain.py:55-56`); msgpack already binarizes the int but stores `mirror_hash` as a hex **string**.

**Packed record (replaces the msgpack list); key (`Codec.hkey(height)` 8B BE) unchanged (C1).**

```
field        | type            | width      | source
-------------|-----------------|------------|------------------------------------------
n_entries    | u16 LE          | 2          | len(entries)
[ per entry ]
 snd_len     | u8              | 1          | len(sender)
 sender      | utf-8           | snd_len    | entry[0]
 rcp_len     | u8              | 1          | len(recipient)
 recipient   | utf-8           | rcp_len    | entry[1]
 amount      | u64 LE          | 8          | entry[2]  -- BIS reward, supply-BOUNDED -> u64 OK (C4)
 mh_len      | u8              | 1          | len(mirror_hash_raw): 20 (blake2b) or 28 (legacy sha224)
 mh          | bytes           | mh_len     | bytes.fromhex(entry[3]) -- RAW, length-prefixed (was 40/56 hex)
```

**No-msgpack-build risk handled (C6 / C0).** Today `reward_chain` routes through `Codec.pack`, which falls back to JSON-separators-encoding when `msgpack` is absent (`kvstore.py:55-62`). The Stage-4 packed record is a **hand-written packer/unpacker**, independent of `Codec`, so byte-identity is the **same bytes on both the msgpack and JSON-fallback builds** (it no longer depends on which Codec backend is compiled in). Per **C6** this JSON/msgpack→binary flip is a **deliberate, fork-gated format break** in the v2 region only, with its own characterization re-baseline. **Reconstruction:** `add(height, sender, recipient, amount_units, mirror_hash)`, `entries_for`, `all_entries`, `balance_delta_units` (`reward_chain.py:50-87`) all see the identical `[sender, recipient, int(amount), mirror_hash_str]` entries — `mh` is re-rendered `mh_raw.hex()` (lowercase) to reproduce the exact 40/56-hex string `entries_for` returned before. `unpack(pack(entries)) == entries`.

---

### 5. fee_dynamics — no store to redesign; one frozen measure that is a CONSENSUS input

`fee_dynamics` keeps **no store of its own**. The next base fee is recomputed every block from `block_store.recent_block_weights(node.last_block, WINDOW, W_UNIT)` (`digest.py:585-593`), a pure function of the canonical (msgpack) block bodies. The post-fork gate is `node.fee_post_fork = fork_height is not None and (node.last_block + 1) >= fork_height` (`digest.py:581-583`) and the per-tx fee check reads `node.base_fee` — **one signal, gate-by-destination-height** (**C0**).

**The one consensus hazard (fix — gated in block_store, flagged here, not silently changed here).** `recent_block_weights` measures openfield as `len(str(r[-1]))` — a **character** count (`block_store.py:155`). This per-block weight feeds the dynamic base fee (`digest.py:593`), which gates per-tx fee acceptance — so it is a **consensus input**. If/when the block-store Stage-4 change stores openfield as **raw bytes**, this MUST switch to `len(r[-1])` (byte count) **in lockstep, at the same `node.fork_height`, gated in the block_store Stage-4 section** — never silently here. For ASCII openfield the two are equal; for any multibyte openfield they differ, which would shift the weight (and the fee) at a height = a real consensus divergence. This domain only **flags** the dependency; the change itself is a block_store concern gated on the single fork signal.

---

### 6. Reconstruction rule (lossless, reversible) — the round-trip proof

For each store: `pack_v2(stored) -> bytes` and `unpack_v2(bytes) -> stored`, selected by the `meta[b"fmt"]` sentinel (**C8**) and the destination-height gate (§7), with `unpack_v2(pack_v2(x)) == x` for every field any reader observes:

- **points / hashes:** `bytes.fromhex(hex) ⇄ raw`; on read re-render `raw.hex()` (always lowercase, matching `.format().hex()`). The shielded `note()`/`_verify_ring` inputs and the reward `entries_for` strings come back byte-identical — so the ring-signature pre-image and the double-spend gate are unchanged.
- **BIS / bounded integers:** `int(decimal_str) ⇄ u64/i64 LE` (`shielded amt/height/pool/flows`, `reward amount`). Readers get the identical Python int.
- **TOKEN / unbounded integers:** `int(decimal_str) ⇄ LEB128 varint` (`cred/deb/tokreg.supply/journal.amt/addrtok/tokset/meta`). No overflow possible.
- **timestamps / txids:** stored as **verbatim length-prefixed UTF-8 text** (no quantization, no hex re-decode). Readers get the identical string.
- **JSON records:** `json.loads ⇄ packed record`, rebuilding the **same dict (keys/values/types)** — including the kind-specific presence/absence of `k`/`a`/`ts`/`prev`/`prev_h` — so `token_detail`, `alias_owner`, `note()`, `tokens_rollback`, `aliases_rollback`, `entries_for` see identical objects.

Because these are **rebuildable projections**, the strongest proof is per-projection (**C7**): drop the env and re-scan from the anchors / snapshot block-store; the new binary env must produce **byte-identical query results** — which the existing rebuild-vs-live parity tests already assert against the JSON/decimal/msgpack stores.

**Per-env state-root & snapshot (C7).** Each projection env (`token_index`, `shieldedv1`, `reward_chain`) carries a sorted-kv **blake2b-256 (digest_size=32)** state-root (template `vm_state.py:97-119`) so it is self-verifying against the tip; snapshots are the **compacted env files** (`mdb_copy --compact`) plus the `{tip_height, tip_hash, fork_height, per-env state-root}` manifest, never a re-serialized dump.

---

### 7. Migration: drop+reindex, no dual-read, parity-gated (fix)

**`notes_h`/`kimg_h` (and `notes`/`kimg`) keys change from embedding the HEX form to raw bytes — the keyspace is incompatible with old data.** A node opening a pre-Stage-4 env under new code would probe raw-byte keys that don't exist (hex keys live there) and **silently miss every lookup**. Therefore, per **C8**, each env carries `meta[b"fmt"] = b"\x04"`; on first open under new code, if the sentinel is absent/lower the projection **force drop+reindexes from a snapshot copy of the block store** (per **C7** / the no-heavy-scans memory — **never a hot full-scan of the 23 GB prod ledger**), and **no dual-read path is provided** (these are cheap rebuildable projections; a drop+reindex is the only supported upgrade). The sentinel is written atomically with the rebuild's first commit.

**Per-projection parity gate (C7).** Before any projection's `*_consensus` read flips to primary, run it in **shadow + `parity_strict`** through `storage_backend.cross_check` extended to assert that projection's invariant, and require `replay_verify` = **0 mismatches** at `fork_height=None` AND over a straddling chain, on the **reconstructed** forms:
- `shieldedv1`: `has_key_image`, `note()`, `pool_units()` vs `ledger_balance(SHIELD_SINK)`.
- `token_index`: `token_balance`, `token_detail`, `aliasget`/`alias_owner`, `tokens_rollback`/`aliases_rollback` undo state vs SQLite `SUM`/`GROUP BY` and the legacy alias maps.
- `reward_chain`: `entries_for`/`balance_delta_units` vs the negative-height-row sum.

Only after that projection passes does its flag flip — never all-at-once.

---

### Dispatch / gate sites (file:line)

- Shielded existence + validate: `digest.py:719-721` (`_sst is not None and _sfh is not None and block_height_new >= _sfh`); apply at `digest.py:797-803`; rollback `shieldedv1.py:450` (driven by chain_ops).
- Token plugin existence: `token_index` config flag (`plugins/tokens_aliases/__init__.py:49`); per-block project `on_block` (`plugins/tokens_aliases/__init__.py:90`); rollback via `node.token_index.*_rollback`.
- reward_chain write/extract: `reward_chain.py:50,89`; balance read path behind its flag.
- fee_dynamics gate: `digest.py:581-583`; weight source `block_store.py:141-157`; consumed `digest.py:593`.
- Single fork signal everywhere: `getattr(node, "fork_height", None)` (set/persisted `digest.py:542-557`). `amounts.LEDGER_INTEGER` (`amounts.py:65`) stays an orthogonal **storage** flag (it only flips `_amount_units` at `shieldedv1.py:506-508` between `int(stored)` and `to_units(stored)`), never a second consensus signal.


**Savings**

| field | legacy form | legacy bytes | true-bytes form | new bytes | saving % |
|---|---|---|---|---|---|
| shielded `notes` key (note_id) | 64-char hex text | 64 | raw sha256 digest 32B | 32 | 50% |
| shielded `notes_h` key (8B height + note_id) | 8B + 64-hex | 72 | 8B + raw 32B | 40 | 44% |
| shielded note `P` (in value) | 66-char hex | 66 | 33B compressed point | 33 | 50% |
| shielded note `R` (in value) | 66-char hex | 66 | 33B compressed point | 33 | 50% |
| shielded note `C` (in value) | 66-char hex | 66 | 33B compressed point | 33 | 50% |
| shielded note JSON framing (keys h/tok/amt/R/P/memo/C + punctuation) | JSON keys+punct | ~60 | packed record framing (ver,lens) | ~16 | 73% |
| shielded note `memo` (typical small AEAD) | base64(nonce+tag+ct) JSON-escaped | ~120 | raw nonce\|\|tag\|\|ct + u32 len | ~88 | 27% |
| shielded `kimg` key (key image) | 66-char hex | 66 | raw 33B compressed point | 33 | 50% |
| shielded `kimg_h` key (8B height + image) | 8B + 66-hex | 74 | 8B + raw 33B | 41 | 45% |
| shielded `flows` value (delta) | decimal string e.g. "100000000000" | ~12 | i64 LE | 8 | 33% |
| shielded `meta` value (pool/counts) | decimal string e.g. "230000000000" | ~12 | i64/u64 LE | 8 | 33% |
| token `cred`/`deb` value (amount, UNBOUNDED) | decimal string e.g. "1000000000" | ~10 | LEB128 varint | ~5 | 50% |
| token `addrtok`/`tokset`/`meta` value (count) | decimal string e.g. "12" | 1–6 | LEB128 varint | 1–3 | ~30% (small) |
| token `tokreg` value | JSON {h,ts,issuer,txid,supply} | ~140 | packed record (u64 h + lp ts/issuer/txid + varint supply) | ~70 | 50% |
| token `journal` value (transfer, WITH ts) | JSON {k,tok,rcp,adr,amt,txid,ts} | ~160 | packed (1B kind + lp strings + varint amt + lp ts) | ~85 | 47% |
| token `journal` value (issue/noop) | JSON {k,...} | ~90 / ~40 | packed (1B kind + lp fields) | ~50 / ~38 | ~45% / small |
| token `ajournal` value (transfer) | JSON {k,al,a,prev,prev_h} | ~90 | packed (1B kind + lp al/a/prev + u64 prev_h) | ~50 | 44% |
| token `ajournal` value (register) | JSON {al,a} | ~40 | packed (1B kind + lp al/a) | ~30 | 25% |
| token `alias_fwd` value | JSON {"a":addr,"h":N} | ~75 | u8 len+addr + u64 height | ~66 | 12% |
| token `seen` key / `tokreg`.txid / `journal`.txid (TEXT, per carve-out) | sig[:56] / blake2b-20 hex text | 40–56 | length-prefixed UTF-8 text (unchanged form) | 41–57 | 0% (JSON-framing removal only where embedded) |
| reward_chain `mirror_hash` (per entry) | 40-hex (blake2b-20) string in msgpack | 40+ | u8 len + raw 20B | 21 | ~48% |
| reward_chain `mirror_hash` legacy sha224 (per entry) | 56-hex string | 56+ | u8 len + raw 28B | 29 | ~48% |
| reward_chain `amount` (per entry, BIS bounded) | msgpack int (already binary) | ~5 | u64 LE | 8 | 0% (parity, not a regression) |


**Adversarial fixes folded in:**
- LOSSY token journal (missing ts): the packed token-journal transfer record now carries ts as a u16-length-prefixed verbatim UTF-8 string (matching the {k,tok,rcp,adr,amt,txid,ts} written at token_index.py:201-202), so token_txs_for_address (token_index.py:275) reconstructs timestamp byte-identically. unpack(pack(j))==j for the transfer kind including ts; issue/noop kinds carry no ts and the unpacked dict also has none.
- LOSSY/INCORRECT ajournal packing: redesigned into three per-kind shapes confirmed against source — register={al,a} with NO 'k' (token_index.py:372), transfer={k:'transfer',al,a,prev,prev_h} (token_index.py:396-397), free={k:'free',al,prev,prev_h} with NO 'a' (token_index.py:414-415). prev_h is a u64 LE INTEGER field (read as int at token_index.py:473,478). unpack rebuilds the exact dict per kind so aliases_rollback (token_index.py:460-481) restores identical alias_fwd/alias_rev state across a register->transfer->free unwind; the free case never carries 'a'.
- u64-LE overflow for TOKEN amounts: all token amount fields (cred, deb, tokreg.supply, journal.amt, addrtok/tokset/meta counters) now use a self-sizing LEB128 unsigned varint, since token amounts are unbounded (token_index.py:164 int(supply) uncapped; _sum_party accumulates int(v) at token_index.py:147). u64 LE is kept ONLY for BIS-denominated bounded fields (shielded amt/height, reward amount). This is the C4 bounded-vs-unbounded rule made normative.
- Kind-tag<->dict reconstruction specified for journal AND ajournal: journal unpacks to the legacy {'k':'issue'|'transfer'|'noop',...} satisfying op['k'] (token_index.py:341-342); ajournal register unpacks WITHOUT a 'k' key so op.get('k','register')=='register' (token_index.py:462), transfer/free carry k='transfer'/'free'. Round-trip unpack(pack(op))==op for every kind, with kind-specific presence/absence of k/a/prev/prev_h.
- Key-image canonicalization before keying: the packer canonicalizes via cc.PublicKey(bytes.fromhex(raw)).format() exactly as shieldedv1.py:625, then asserts len==33 BEFORE computing any raw kimg/kimg_h key; a 65-byte uncompressed point is refused so the has_key_image spent-set dedup (shieldedv1.py:378-380,768,793) cannot be bypassed.
- Memo cap confirmed and prefix sized safely: transparent cap 4096 (shieldedv1.py:612), v3 confidential cap 65536 base64 chars (shieldedv1.py:579). u16 (max 65535) would truncate the max v3 memo, so the notes record stores RAW decoded ciphertext (<=49152 bytes) with a u32 LE length prefix; on read b64encode(nonce||tag||ct) reproduces the exact canonical base64 string _decrypt_memo slices (shieldedv1.py:163-164). Truncation is structurally impossible.
- notes_h/kimg_h keyspace incompatibility: mandated per-projection drop+reindex-from-snapshot with NO dual-read, guarded by the C8 meta[b'fmt']=b'\x04' sentinel (a stale pre-Stage-4 env force-rebuilds instead of silently missing raw-byte keys), and a per-store parity gate (note(), has_key_image, token_balance, aliasget, reward entries_for) asserting byte-identical rebuilt-from-snapshot vs live results through storage_backend.cross_check with 0 replay_verify mismatches over a straddling chain BEFORE the layout-version flag flips (C7).
- fee_dynamics/block_store openfield measure: explicitly flagged that recent_block_weights uses len(str(r[-1])) char count (block_store.py:155) which feeds the consensus base fee (digest.py:593), so any switch to len(bytes) must happen in lockstep with the block-store openfield-raw-bytes change at the SAME node.fork_height, gated in the block_store Stage-4 section — NOT silently in this domain. This domain only declares the dependency.
- CORRECTED reward_chain mirror_hash form: the original design's 'sha224 28B' was wrong — calculate_mirror_hash is blake2b(digest_size=20).hexdigest() (digest.py:898) = 40-hex/20 raw bytes; legacy SQLite rows may carry sha224 (56-hex/28B). The mirror hash is now stored length-prefixed (u8 len + raw bytes) to losslessly cover both forms across a rebuild from a mixed ledger.
- CORRECTED C3 reconciliation for token txids: documented the explicit carve-out that token_index seen/tokreg.txid/journal.txid are the plugin's signature[:56]/blake2b-20-hexdigest TEXT feed (plugins/tokens_aliases/__init__.py:28-29,138,145), NOT the C3 raw-32 content-txid, so they stay length-prefixed UTF-8 text (no forced hex re-decode / A-hex-in-reverse). The C3 raw-32 dedup is txid_index's responsibility, not this store's; moving the feed is a separate fork-gated re-baseline per C6.
- ts quantization risk removed: tokreg.ts and journal.ts are stored as VERBATIM length-prefixed UTF-8 strings (the raw tx[_TS] feed, plugins/tokens_aliases/__init__.py:138,158), not centiseconds, so they round-trip byte-identically regardless of the timestamp's dp format — eliminating the round(ts*100) re-render hazard the adversary flagged.


**Implementation checklist (residual review items to satisfy before wiring this domain):**
- [ ] Shielded `C` field: make it length-discriminated, NOT fixed 33B. Either (a) length-prefix `C` (u8 len + raw bytes) so 32B sha256 (v1/v2) and 33B Pedersen point (v3) both round-trip, or (b) key the C width off the `ver` byte (ver in {1,2} -> 32B from commitment() sha256; ver==3 -> 33B point). Prove `unpack(pack(d))['C'].hex() == d['C']` for BOTH a transparent note (64-hex) and a v3 note (66-hex).
- [ ] Shielded `notes` `amt`: change from u64 LE to a LEB128 unsigned varint (or u128) -- shielded notes carry arbitrary tokens (tok up to 64 chars, shieldedv1.py:577,610), so `amt` is an UNBOUNDED token amount exactly like token_index. u64 LE remains valid ONLY if the spec ALSO constrains shielded notes to tok=='bis'; it does not. Keep u64 only for the genuinely-bounded `height` field.
- [ ] reward_chain `mh`: store the mirror_hash VERBATIM as a length-prefixed UTF-8/raw-bytes field (like the token-txid carve-out), NOT via bytes.fromhex(). It must losslessly round-trip the placeholder '0', the empty string '', and any non-even/non-hex value, since `extract_from_ledger` lifts every block_height<0 row's r[7] column including vm:payout/shield:payout/staking mirror rows (dbhandler_write.py:178,190; staking.py:162,172). Re-render the EXACT original string on read.
- [ ] token/alias slots: change the `tok`/`al`/`a`/`prev`/`rcp`/`adr`/`txid` (and any address) length prefixes from u8 to u16 or varint in tokreg/journal/ajournal/cred-deb records, because token names and aliases are uncapped (derived from the free-text openfield). Either that, or add+enforce a documented consensus length cap in token_index before packing -- but a silent u8 truncation must be impossible. Add a characterization test with a >255-byte token name.

---

## 10. Mempool storage  `[mempool]`

## Stage-4 TRUE-BYTES Mempool Storage (domain: mempool)

### 0. Scope and the one thing that makes mempool different

The mempool is a **non-consensus, ephemeral admission buffer**. It is rebuilt from peers on every restart, purged after `REFUSE_OLDER_THAN = 7200s` (`mempool_sql.py:17`), and never participates in a block hash or a signature pre-image. Nothing stored here is replayed; nothing here is byte-frozen by `tests/test_characterization.py`. Therefore the mempool record may adopt the binary true-bytes form **immediately, with NO fork gate** — see §6 for the precise reason this does not violate the one-fork-signal invariant.

The hard constraints that DO apply are loss-lessness and codec-reuse:

1. The exact 8-field wire tuple that every consumer reads today —
   `(timestamp, address, recipient, amount, signature, public_key, operation, openfield)` —
   must be reconstructed **byte-identically** on read. Consumers: `mempool_queries.py:110` (`SQL_SELECT_ALL_VALID_TXS`), `miner.py:102` (`_build_block`), `node.py:620,642`, `apihandler.py:67`, `rest_api.py:1368`, `rest_stats.py:548`, and the digester via the candidate block. In that tuple `timestamp` is the `'%.2f'` string, `amount` is the `'%.8f'` string, and `signature`/`public_key` are **base64 ASCII text** (exactly what `SignerFactory.verify_tx_signature` / `b64decode` consume — `polysign/signerfactory.py:218,243,247`).
2. We must **reuse, not duplicate, the ledger codec.** The integer/binary field encoders already exist in `bismuth_serialize.py` (the `_v2_*` family) and the field-narrowing + storage-mode-agnostic amount handling already exists in `txid_index.py`. The mempool record reuses those helpers verbatim; it does not invent a second amount/timestamp codec.

### 1. Store shape

A single LMDB env (constructed through the engine seam, exactly like the other 8 stores) keyed by **raw 32-byte content txid**. For a RAM mempool (`config.mempool_ram`, `mempool.py:62-65`, regnet) the same record bytes live in a plain `dict[bytes32, bytes]` — the record codec is identical, only the container differs, so there is one serializer for both paths.

```
open_store(backend, path, dbs=["mp"], map_size=2*GIB)     # kvstore.open_store, kvstore.py:536
  db "mp":  key = txid32 (raw 32 bytes)   ->   value = MEMREC bytes (see §2)
```

Why the raw 32-byte txid as key (not the signature):
- It is the same canonical content id the ledger and `txid_index` already use (`bismuth_serialize.tx_id_at`, `txid_index.txid_of`), so the ledger-membership reject in `mempool.py:421-428` becomes one `txid_index.contains(txid)` O(1) lookup instead of the `substr(signature,1,4)` scan.
- 32 raw bytes vs the legacy dedup key of `signature` (~680 char base64 text for RSA). The current SQL dedup index is a prefix on an 8 KB-wide TEXT column (`mempool_sql.py:41`).
- LMDB key max is 511 B; 32 B is far under it (no hashing needed, unlike block_store's pubkey key, `block_store.py:76-79`).

Pre-fork note: pre-fork the canonical id is still the legacy signature slice. The mempool key is computed by `tx_id_at(dest_height, fork_height, ...)` (`bismuth_serialize.py:166`), so pre-fork it is the legacy `tx_id` (blake2b of the frozen `signature_buffer`) — a content id that is **still 32 raw bytes** and still a valid unique mempool key. The KEY codec is fork-aware purely for cross-store consistency with `txid_index`; the RECORD codec (below) is not (§6).

### 2. Record layout (MEMREC)

All multi-byte integers little-endian; every variable field length-prefixed; the two amount/time fields stored as native integers and reconstructed to their frozen string forms on read. Signature and public_key are stored as **TRUE raw bytes** (base64-decoded), not the base64 text and not hex — this is the whole win, and it is legitimate because the value is an LMDB value-blob (or a dict value), never a text column (avoids the A-hex / base64 regression).

```
MEMREC value bytes
field          | type                     | width      | notes
---------------|--------------------------|------------|-------------------------------------------
magic          | u8                       | 1          | 0xB3  (0xB2 is V2 pre-image MAGIC; 0xB3 distinct, see §3)
version        | u8                       | 1          | 0x01
flags          | u8                       | 1          | bit0 = sig_is_raw, bit1 = pub_is_raw, bit2 = pub_empty
ts_cs          | u64                      | 8          | integer centiseconds (== bismuth_serialize._v2_ts_cs(timestamp))
amount_units   | u64                      | 8          | integer atomic units (== bismuth_serialize._v2_units(amount))
addr_len       | u8                       | 1          | <= 56
address        | bytes                    | addr_len   | UTF-8 (ascii base58/hex address)
recip_len      | u8                       | 1          | <= 56
recipient      | bytes                    | recip_len  | UTF-8
op_len         | u8                       | 1          | <= 30 (digest_tx truncation)
operation      | bytes                    | op_len     | UTF-8
sig_len        | u16                      | 2          | raw signature byte length (RSA ~512, ML-DSA-87 ~4627)
signature      | bytes                    | sig_len    | RAW signature bytes (base64-DECODED). flags.bit0=1
pub_len        | u16                      | 2          | raw pubkey byte length; 0 if flags.bit2 (recoverable/dropped)
public_key     | bytes                    | pub_len    | RAW pubkey bytes (base64-DECODED). flags.bit1=1
of_len         | u32                      | 4          | <= 100000 (V2_OPENFIELD_MAX)
openfield      | bytes                    | of_len     | UTF-8 / raw payload
mergedts       | u32                      | 4          | unix seconds merge time (was the SQL mergedts column)
```

`flags` exists for one robustness reason: a few legacy/edge txs carry a signature or pubkey that is **not valid base64** (e.g. the historic `"b'"`-prefixed pubkey leftover handled at `mempool.py:359-361`). For those, `sig_is_raw`/`pub_is_raw` is set to 0 and the field is stored as the **UTF-8 bytes of the original text** instead of base64-decoded bytes; on read it is re-emitted as text verbatim. Normal txs set the raw bits and round-trip through base64. This keeps the record total and reversible for 100% of admitted txs without a lossy "must be base64" assumption. `pub_empty` (bit2) covers post-fork secp256k1/ED25519 where the wire pubkey field is the empty string (recovered from address, doc/18 §A.1, doc/29 §2.C).

### 3. Codec reuse (do NOT duplicate)

The integer field encoders are taken straight from the frozen module — no new amount/time math in mempool code:

```
ts_cs        = bismuth_serialize._v2_ts_cs(timestamp)     # '%.2f' -> centiseconds   (bismuth_serialize.py:101)
amount_units = bismuth_serialize._v2_units(amount)        # '%.8f' -> atomic units    (bismuth_serialize.py:106)
```

and the length-prefix primitive is the same `_v2_lp(b, width)` used by the ledger pre-image (`bismuth_serialize.py:72`). The mempool record is built from the **same six content fields** the ledger encodes, plus the two carried-but-not-signed fields (signature, public_key) encoded as raw bytes exactly as the Stage-4 ledger record encodes them (the raw-byte sig/pubkey refinement of `_v2_tx_bytes`, doc/29 §2.C — the mempool adopts that sibling encoder rather than the still-base64 interim at `bismuth_serialize.py:124-125`). MAGIC is `0xB3`, deliberately one more than the pre-image `V2_MAGIC=0xB2` (`bismuth_serialize.py:67`): a mempool record can never alias a signing pre-image even if a buffer were mis-routed.

The KEY is computed by reusing `txid_index`'s exact field helper so the mempool and the ledger index agree bit-for-bit:

```
txid32 = bytes.fromhex(
    bismuth_serialize.tx_id_at(dest_height, fork_height,
        timestamp, address, recipient, amount, operation, openfield))   # txid_index._txid_fields, txid_index.py:35
# dest_height = confirmed_tip + 1  (the height this tx would land at)
```

### 4. Reconstruction rule (lossless, proven field by field)

`read(txid32) -> (timestamp, address, recipient, amount, signature, public_key, operation, openfield)`:

```
timestamp  = '%.2f' % (ts_cs / 100)             # exact inverse of _v2_ts_cs; ts_cs is integer centiseconds
amount     = amounts.from_units(amount_units)   # amounts.py:28 -> exact '%.8f' string (integer formatting, no float)
address    = address_bytes.decode('utf-8')
recipient  = recip_bytes.decode('utf-8')
operation  = op_bytes.decode('utf-8')
openfield  = of_bytes.decode('utf-8')
signature  = base64.b64encode(sig_bytes).decode() if flags.sig_is_raw else sig_bytes.decode('utf-8')
public_key = ''                                  if flags.pub_empty
             else (base64.b64encode(pub_bytes).decode() if flags.pub_is_raw else pub_bytes.decode('utf-8'))
```

Why this is exactly lossless:
- **timestamp**: the wire value is always the `'%.2f'` quantized string (`mempool.py:327`, `quantize_two`). `_v2_ts_cs` multiplies the quantized Decimal by 100 to an exact integer (`bismuth_serialize.py:103`), so `ts_cs/100` re-formatted `'%.2f'` is identical. No float widening (ts_cs is an int divided then formatted to 2 dp).
- **amount**: the wire value is the `'%.8f'` quantized string (`mempool.py:346`). `_v2_units` → integer atomic units (`bismuth_serialize.py:109`); `amounts.from_units` is the exact integer→`'%.8f'` inverse used everywhere on the consensus reconstruction path (`amounts.py:28-36`), so it is byte-identical and never loses precision above 2^53 (the explicit invariant in `amounts.consensus_amount`).
- **signature / public_key**: for the normal (base64) case, `b64encode(b64decode(x)) == x` holds because the wire form is canonical base64 produced by the signer (`miner.py:108`, `signer_*.py`) — no whitespace/newline variants — so the round trip is exact. The `flags.*_is_raw=0` escape hatch stores the original text bytes verbatim for the rare non-canonical case, guaranteeing 100% reversibility regardless.
- **address/recipient/operation/openfield**: stored as their UTF-8 bytes, decoded back — identity.
- **mergedts**: carried so the peer-relay queries that order by/filter on merge time (`SQL_SELECT_TX_TO_SEND_SINCE`, `mempool_queries.py:168`) and the 2-hour purge are preserved; the public 8-field tuple drops it exactly as the SQL `SELECT` list does today (`mempool_sql.py:49`).

Because the read reproduces the precise 8-field tuple, every existing consumer — the signature verifier (`SignerFactory.verify_tx_signature`), the block builder (`miner.py:102`), the digester, and the REST/peer relay — is unchanged and bit-compatible. The signing/verification pre-image is rebuilt by those consumers from the reconstructed string fields exactly as today; the mempool storage form is fully decoupled from the consensus pre-image form (reconstruction discipline).

### 5. Operations mapping (replacing mempool_sql.py)

| legacy SQL (`mempool_sql.py`) | true-bytes op |
|---|---|
| `INSERT INTO transactions VALUES(...)` (`mempool.py:508`) | `txn.put(mp, txid32, MEMREC)` |
| `SQL_SIG_CHECK` / `sig_check` (`mempool_sql.py:41`) | `txn.get(mp, txid32) is not None` (O(1), no substr index) |
| `SQL_DELETE_TX` (`mempool_sql.py:45`) | `txn.delete(mp, txid32)` — delete-by-txid after a tx is mined |
| `SQL_SELECT_ALL_VALID_TXS` (`mempool_sql.py:52`) | `iterate(mp)` decoding each MEMREC, filter `mergedts > now-7200` |
| `SQL_PURGE` (2h) (`mempool_sql.py:35`) | `iterate(mp)`, delete where `mergedts <= now-7200` |
| `SQL_SELECT_TX_TO_SEND ORDER BY amount DESC` | `iterate(mp)` then sort by `amount_units` in memory (mempool is small/bounded) |
| ledger-membership reject (`mempool.py:421-428`) | `txid_index.contains(txid32)` (`txid_index.py:118`) |

Ledger-dedup against the **ledger encodings is reused, not duplicated**: the same `txid_index` projection the digester maintains is queried directly — the mempool no longer runs its own `signature LIKE` scan over the ledger.

### 6. Fork gating — why mempool needs none, and how pre-fork stays byte-identical

This is the crux. The mempool RECORD is **not gated** and that is correct, not a violation:

- The one-fork-signal invariant governs **consensus bytes** — signature pre-images, txids, block hashes — which must be reproduced byte-identically for replay. The mempool record is none of those: it is never hashed into a block, never signed, never replayed. It is reconstructed to the 8-field tuple before any consensus function touches it, and that reconstruction is exact (§4). So the storage form can be binary at all heights without changing a single consensus byte.
- **Pre-fork byte-identity is preserved by construction** because the consensus path never sees the record. When a tx is read out, the verifier rebuilds the legacy `signature_buffer` / `tx_id` from the reconstructed `'%.2f'`/`'%.8f'` strings exactly as it does from the current SQL row — identical inputs, identical bytes. The mempool storage rewrite is invisible to `replay_verify.py` (which never reads the mempool) and to `tests/test_characterization.py` (which locks the frozen functions, untouched here).
- The **one place a height matters** is the KEY, and it is gated by the existing single signal, by DESTINATION height: `txid32 = tx_id_at(dest_height, fork_height, ...)` with `dest_height = confirmed_tip + 1` and `fork_height = mp.MEMPOOL.fork_height` (already mirrored from `node.fork_height` at `digest.py:565-566`). With `fork_height is None` (mainnet today, until on-chain lock-in) this is the legacy `tx_id` — so 100% of current keys take the frozen legacy branch by construction (None-means-legacy). This reuses the dispatcher at `bismuth_serialize.py:166`; it introduces NO second signal. The record body's amount/time integer encoding is orthogonal to consensus (it is `amounts.LEDGER_INTEGER`-style storage, decoupled from the height gate), so it does not need the gate at all.

The admission-time signing-scheme selection already binds to destination height via `mempool_post_fork = (tip+1) >= fork_height` (`mempool.py:393-399`) and stays exactly as-is — the storage rewrite changes how an admitted tx is persisted, not how it is verified.

### 7. Dispatch / gate site

- KEY codec gate (single signal, by destination height): `bismuth_serialize.tx_id_at(...)` at **bismuth_serialize.py:166-175**, called by mempool put/get with `fork_height = mp.MEMPOOL.fork_height` (set at **digest.py:565-566**) and `dest_height = confirmed_tip + 1`.
- Field codec reuse: `_v2_ts_cs` **bismuth_serialize.py:101**, `_v2_units` **bismuth_serialize.py:106**, `_v2_lp` **bismuth_serialize.py:72**, `amounts.from_units` **amounts.py:28**.
- Store seam: `kvstore.open_store` **kvstore.py:536**.
- Ledger-dedup reuse: `txid_index.contains` **txid_index.py:118**, `txid_index.txid_of` / `_txid_fields` **txid_index.py:35-50**.
- Code that changes: the SQL definitions in **mempool_sql.py:30-69** (replaced by the MEMREC codec + KV ops); the insert at **mempool.py:508-511**; the dedup reads at **mempool.py:412,421-428**; and the read queries that compose the 8-field tuple in **mempool_queries.py:110,155,164,168**. Consumers (`miner.py:102`, `node.py:620,642`, `apihandler.py:67`, `rest_api.py:1368`) are unchanged — they keep receiving the identical 8-field tuple.


**Savings**

Per-row sizing uses the inventory's typical mempool row (RSA-class tx, ~500 B openfield). Legacy bytes are the SQLite TEXT-column widths from the legacy-sql inventory; true-bytes are the MEMREC field widths from §2.

| field | legacy form | legacy bytes | true-bytes form | new bytes | saving % |
|---|---|---|---|---|---|
| key / dedup | signature TEXT (base64, substr-indexed) ~680 | 680 | raw 32-byte txid | 32 | 95.3% |
| timestamp | '%.2f' TEXT | 19 | u64 ts_cs | 8 | 57.9% |
| address | 56-char hex TEXT | 56 | u8 len + 56 raw | 57 | -1.8% |
| recipient | 56-char hex TEXT | 56 | u8 len + 56 raw | 57 | -1.8% |
| amount | '%.8f' TEXT | 18 | u64 amount_units | 8 | 55.6% |
| signature | base64 TEXT (RSA ~512 B -> ~680 chars) | 680 | u16 len + 512 raw | 514 | 24.4% |
| public_key | base64 TEXT (RSA ~600 chars) | 600 | u16 len + ~450 raw | 452 | 24.7% |
| operation | TEXT (<=30) | 8 | u8 len + 8 raw | 9 | -12.5% |
| openfield | TEXT (~500) | 500 | u32 len + 500 raw | 504 | -0.8% |
| mergedts | INTEGER as text | 10 | u32 | 4 | 60.0% |
| record framing | SQLite row + per-col header overhead | ~50 | magic+version+flags | 3 | 94.0% |
| **row total (incl. dedup key)** | **~2,727** | **~1,648** | **~39.6%** |
| **ML-DSA-87 tx (sig 4627, pub 2592, base64 ~33% bloat)** | sig ~6172 b64 + pub ~3456 b64 + ~120 other | **~9,748** | raw 4627 + raw 2592 + ~120 + framing | **~7,343** | **24.7%** |


**Implementation checklist (residual review items to satisfy before wiring this domain):**
- [ ] Do NOT route the ledger-membership reject through txid_index when fork_height is None. Either (a) keep the existing legacy ledger signature/timestamp check on the pre-fork path and only use txid_index.contains once fork_height is set AND the txid_index is fully populated for [0, tip] (it is not pre-fork by construction), or (b) gate the substitution on fork_height is not None and fall back to the legacy ledger scan otherwise. The current §5/§7 mapping must not be shipped as-is.
- [ ] Correct §1 and risk #4: the RAM mempool is in-memory SQLite, not a dict. Re-specify how the single MEMREC codec is exercised on the actual RAM path, and write the regnet+mainnet parity test against the real container, not a hypothetical dict.
- [ ] Preserve legacy purge/valid-select semantics: keep purge and SQL_SELECT_ALL_VALID_TXS keyed on the tx 'timestamp' field (or explicitly justify and document the switch to mergedts for both, and update REFUSE_OLDER_THAN handling consistently). Do not silently change tx-time staleness to merge-time staleness.
- [ ] Make the admission-time base64 round-trip assert MANDATORY (risk #1): on every admit, assert b64encode(b64decode(sig))==sig (and same for pubkey) before setting sig_is_raw/pub_is_raw=1; on failure store via the text-bytes path. Lossless reconstruction otherwise relies on an unverified canonicality assumption.
- [ ] Resolve the encoder dependency (open_question 3): land the raw-byte sig/pubkey sibling encoder as ONE shared function before the mempool claims verbatim reuse, so the mempool and the future ledger Stage-4 record cannot diverge into two raw-encoder implementations.

---

## 11. Difficulty / cumulative-work store (the missing misc table)  `[difficulty]`

## Domain: Difficulty / cumulative-work store (the missing `misc` table)

> Obeys SHARED CROSS-DOMAIN CONVENTIONS C0–C8 verbatim. Where this section and the
> shared section appear to disagree, the shared section wins.

### D.0 Why this domain exists (the gap)

The SQLite `misc` table — `CREATE TABLE misc (block_height INTEGER, difficulty TEXT)`
(`regnet.py:43`, `migrate_amounts.py:29`, declared a gap at doc/26:179) — is the
**only** consensus-relevant per-block datum that the LMDB rearchitecture never
gave a home. The block_store value `{"h": hash, "t": [txs]}` (`block_store.py:7`)
carries **no** difficulty, **no** solvetime, **no** cumulative work, and neither
does any projection (`balance_index` / `txid_index` / `token_index` /
`reward_chain` / `vm_state` / `shieldedv1`). Yet:

- **PoW verify reads it every block.** `digest.py:618` calls `difficulty(node, db_handler)`,
  which at `difficulty.py:70-71` does `SELECT difficulty FROM misc ORDER BY block_height DESC LIMIT 1`
  → `Decimal(...)` as `diff_block_previous`, the seed for both the legacy controller
  and the post-fork LWMA retarget. The returned `diff[0]` is the threshold
  `verify_proof_of_work` checks against (`digest.py:421`, `mining_heavy3.check_block`).
- **The post-fork LWMA needs the prev difficulty too.** `difficulty.py:106-112`:
  `difficulty_lwma.lwma_next_difficulty(solvetimes, diff_block_previous, ...)`. The
  solvetimes come from miner-tx timestamps (`difficulty.py:30-38`,
  `_recent_solvetimes`), but `diff_block_previous` is **still a `misc` read**.
- **Write path stamps it every block.** `digest.py:726` →
  `dbhandler_write.to_db(block_array, diff_save, ...)` →
  `INSERT INTO misc VALUES (?, ?)` (`dbhandler_write.py:218-219`).
- **REST/socket API reads it.** `apihandler_blocks.py:93-95` (`getblockfromhashextra`,
  i.e. "hashextra") and `:158-162` (`getblockrange`).
- **Rollback deletes it.** `DELETE FROM misc WHERE block_height >= ?`
  (`dbhandler_write.py:50,64,78,84`; `chain_ops.py:721,773`).
- **Fork-choice has no work column at all.** Today chain selection is height-based;
  there is no `cumulative_work` anywhere in the tree (`grep` for
  `cumulative_work|chain_work|total_work|chainwork` returns nothing). A true
  most-work fork rule needs it, and the natural place to materialize it is here,
  next to the per-block difficulty it is derived from.

Until `misc` has an LMDB home that is **byte-identical on reconstruction**, the
SQLite trio (H1/H2/H3) cannot be retired: `misc.difficulty` is a live
consensus-relevant read.

### D.1 Store layout — one new projection env `diff_store`

Per **C7**, this is a **rebuildable projection** (every value is derivable from the
canonical block_store + the frozen difficulty/LWMA functions), so it lives in its
**own separate env** with its own state-root and its own `meta[b"fmt"]` sentinel
(**C8**). It is opened through `kvstore.open_store` exactly like the other
projections (`open_store(backend, path, dbs=[...], map_size=..., lock=True)`,
`kvstore.py:536`; `BlockStore.__init__` pattern `block_store.py:52-58`).

Two sub-dbs:

| db | key | value | width | codec |
|---|---|---|---|---|
| `diff` | `Codec.hkey(height)` — **8B BE uint64** (C1) | packed fixed record (D.2) | **28B fixed** | raw bytes, no codec (C6) |
| `meta` | `b"fmt"` / `b"root"` / `b"anchor"` — raw ASCII | see D.5/C8 | var | raw bytes |

Keying by `Codec.hkey` (C1) makes `diff` byte-lexicographic == numeric, so the
LWMA window read (D.3) and the tip read (D.4) are O(window) / O(1) ordered range
scans, identical on LMDB / MDBX / sqlite-kv backends.

**C1 keyspace-aliasing invariant.** `diff` keys are **real positive block
heights only** — exactly like `block_store.blocks`. There is **no** negative-height
or reward-mirror row in `diff` (the legacy `misc` table itself never had negative
heights; only `transactions` does). `diff_store` therefore lives in a separate env
from `reward_chain` (whose keys are negated-from-negative), and they can never
collide. No env contains both a `diff`-class key and a `reward_chain.rewards`-class
key.

### D.2 Packed record — exact byte layout (true bytes, C0 A-hex ban)

Each `diff[hkey(height)]` value is a **fixed 28-byte little-endian record**. It
stores the consensus difficulty in a lossless integer form, the inter-block
solvetime as an integer, and the running cumulative chain work — none as text.

```
offset  size  field            type        meaning
------  ----  ---------------  ----------  ------------------------------------------------
  0      8    difficulty_e10   u64 LE      difficulty * 10**10, rounded (the quantize_ten grid)
  8      4    solvetime        u32 LE      this block's solvetime in seconds (ts - prev_ts), clamped >=0
 12     16    cumulative_work   u128 LE     running Σ work over [genesis..this height], integer work units
------  ----
 28 bytes total, struct format "<QIQQ"  (note: the 16B u128 is written as two u64 LE limbs, lo then hi)
```

Field rationale, each pinned to source reality:

- **`difficulty_e10` (u64 LE).** Difficulty is a **log2 work-domain Decimal**
  quantized to **10 dp** by `quantize_ten` (`quantizer.py:47-53`;
  `difficulty.py:99,111`). The grid is therefore exactly `value * 10**10`. The
  largest plausible difficulty (a few hundred) × 10¹⁰ is ~10¹² — far under
  u64 max (~1.8×10¹⁹), so u64 holds the entire grid with **zero precision loss**.
  This integer **is** the consensus difficulty; it is not a lossy display form.
  (Storing the *float* would be the bug — see D.6 on why the TEXT reconstruction
  uses the **original string**, not this number, for legacy byte-identity.)
- **`solvetime` (u32 LE).** The inter-block time `ts(height) - ts(height-1)` in
  **integer seconds**, clamped to `>= 0` (a non-monotonic timestamp stores `0`,
  which the LWMA then re-clamps to its own `[1, clamp*target]` window per
  `difficulty_lwma.py:64-67` — storage never pre-applies the LWMA clamp, it only
  prevents an underflow). u32 covers ~136 years between blocks; a 60s target makes
  overflow impossible in practice. Genesis (height 1) has no predecessor →
  `solvetime = 0`. This is a **convenience/acceleration** field: it lets the LWMA
  window read (D.3) avoid re-reading miner-tx timestamps out of block_store, but
  it is **not** a new consensus input — `difficulty_lwma` still defines the law and
  the value is recomputable from block_store timestamps, so it is covered by the
  state-root (D.5) and the cross-check (D.7).
- **`cumulative_work` (u128 LE, two u64 limbs).** Bismuth difficulty is a LOG2
  work domain: `work(block) ~ 2**(difficulty/2)` (`difficulty.py:84`
  `pow(2, diff/2)`; `difficulty_lwma.py:15-16` "work ~ 2**(diff/2)"). To keep
  cumulative work an **exact integer** (no float drift across a 23GB chain) we
  define a fixed-point integer work unit:

  ```
  work_units(height) = floor( 2 ** (difficulty_e10 / (2 * 10**10) * SCALE_BITS) ... )   # see D.2.1
  cumulative_work(height) = cumulative_work(height-1) + work_units(height)
  ```

  u128 (16 bytes) is mandated, **not** u64: summed over millions of blocks at
  difficulty ~100+ the running total comfortably exceeds 2⁶⁴. This mirrors **C4's
  "bounded vs unbounded width"** discipline — cumulative work is *unbounded-growing*,
  so u64 would be an overflow bug, not a size win. (We use u128, written as two
  LE u64 limbs, rather than a varint, so the record stays fixed-width 28B and the
  tip read is a single positioned get.)

#### D.2.1 The exact integer work-unit definition (determinism, C0)

`2 ** (difficulty/2)` is a non-integer real; computing it in float forks nodes
(libm last-bit divergence — the exact hazard `difficulty_lwma.py:18-22` calls out
for `math.log2` vs `Decimal.ln`). So `work_units` is defined **only** in the
Decimal domain under a **fixed local precision**, reusing the difficulty module's
own deterministic discipline:

```python
# diff_work.py  (new; pure, unit-tested; NOT wired into consensus until the gate, D.8)
from decimal import Decimal, localcontext
_WORK_PREC = 80          # >> the ~13 significant digits the result needs; isolates from caller ctx
_WORK_SHIFT = 2**64      # fixed-point scale: store work as floor(real_work * 2**0)?  -> see note

def work_units(difficulty_e10: int) -> int:
    """Deterministic integer work for one block. difficulty_e10 = difficulty*10**10."""
    with localcontext() as ctx:
        ctx.prec = _WORK_PREC
        d = Decimal(difficulty_e10) / Decimal(10**10)        # exact: integer / 10**10
        # work = 2**(d/2) = exp( (d/2) * ln 2 ).  Decimal.exp / Decimal.ln are correctly
        # rounded to ctx.prec (decimal spec) -> bit-identical on every platform, unlike libm.
        w = (Decimal(2).ln() * d / Decimal(2)).exp()
        return int(w.to_integral_value(rounding="ROUND_FLOOR"))   # floor -> deterministic integer
```

`to_integral_value(ROUND_FLOOR)` yields the canonical integer; at difficulty ~100
that integer is ~10¹⁵, so a single block's work is itself well within u64 and the
**sum** is what needs u128. Because everything is `Decimal` under a pinned `prec`,
two nodes computing `work_units` for the same `difficulty_e10` get the **identical**
integer — the same guarantee `difficulty_lwma` relies on. (`_WORK_SHIFT` is unused
in this floor-of-real-work form; left as a hook only if a future stage wants
sub-integer resolution — it would be a fork-gated format break per C6, re-baselined,
never a silent change.)

### D.3 LWMA range-read of the last N

`difficulty.py:109` feeds `difficulty_lwma.lwma_next_difficulty(solvetimes,
diff_block_previous, ...)` where `WINDOW = 60` (`difficulty_lwma.py:30`). With
`diff_store`:

- **`diff_block_previous`** = decode `difficulty_e10` from `diff[hkey(tip)]`,
  rebuild the Decimal via `Decimal(difficulty_e10) / Decimal(10**10)` (lossless,
  the exact `quantize_ten` grid value). This replaces
  `difficulty.py:70-71`'s `SELECT difficulty FROM misc ... LIMIT 1`.
- **`solvetimes`** (oldest→newest) = an ordered LMDB range scan over the last
  `WINDOW` keys: position a cursor at `hkey(tip - WINDOW)` and walk forward to
  `hkey(tip)`, reading the `solvetime` (u32 LE at offset 8) from each record. C1's
  big-endian keys make this a contiguous lexicographic range. This is the
  acceleration over `_recent_solvetimes` (`difficulty.py:30-38`), which scans
  miner-tx timestamps; both yield the **same** `[ts[i]-ts[i-1]]` sequence (proven
  by D.7 cross-check), but the projection read is a single positioned scan of
  fixed-width records, not a `SELECT ... WHERE reward != 0` over `transactions`.

Genesis / short-chain behavior is preserved: if fewer than `WINDOW` rows exist the
range scan returns what is there, exactly as `[-window:]` slicing in
`difficulty_lwma.py:53` already tolerates.

### D.4 Fork-choice read of cumulative work at the tip

Most-work fork choice reads **one** record:

```
tip_work = u128(diff[hkey(tip_height)][12:28])   # lo limb [12:20], hi limb [20:28], LE
```

Because keys are BE (C1), `tip_height` is the **last** key in `diff` — an O(1)
`cursor.last()`. Comparing two candidate chains is `tip_work_A` vs `tip_work_B` as
plain Python ints (u128 decoded). This is the data fork-choice needs and currently
**cannot** get (no work column exists today). Until a most-work rule is actually
switched on, `cumulative_work` is written-and-verified but unread by consensus
(inert, exactly like `difficulty_lwma` is "pure + not wired" per its own docstring,
`difficulty_lwma.py:24-25`); enabling most-work selection is a separate, explicitly
scoped change and is **not** assumed here.

### D.5 State-root (C7) and `meta`

`meta[b"fmt"] = b"\x04"` (Stage-4 sentinel, C8). On open under new code, an
absent/lower sentinel forces a `drop+rebuild` of `diff` from the canonical
block_store snapshot (C7/C8) rather than silently missing raw-byte keys that an
old hex-keyed copy wouldn't contain.

`meta[b"anchor"] = Codec.hkey(last_indexed_height)` — the projection's tip pointer,
advanced atomically with each block's `diff` write (mirrors `token_index`'s anchor
discipline).

`meta[b"root"]` = a **blake2b-256 (digest_size=32, C2) state-root** over the sorted
`diff` kv pairs (template `vm_state.py:97-119`): feed `hkey || 28B-record` for every
height in ascending key order into one `blake2b(digest_size=32)`. This makes the env
self-verifying against the tip block and lets a snapshot manifest
(`{tip_height, tip_hash, fork_height, diff_root}`, C7) prove integrity without a
re-scan.

### D.6 Reconstruction of the legacy `misc.difficulty` TEXT — byte-for-byte (C0)

This is the load-bearing correctness rule. **The legacy `misc.difficulty` value is
`str()` of whatever Python object `diff_save` was** — and that is *not* a fixed
numeric format:

- Normal path: `diff_save = diff[0] = float('%.10f' % difficulty)` (`difficulty.py:139`),
  bound to the TEXT column and read back as a **string** like `"118.4732615334"`,
  `"16.0"`, `"50.0"` (verified: `INSERT ... VALUES(?, 16.0)` → `SELECT` returns the
  *string* `"16.0"`).
- New-chain / regnet-init except-fallback returns the **int** `24` (`difficulty.py:143`
  `[24,24,...]`), so `str(24) == "24"` (NOT `"24.0"`).
- Regnet stores `str(float(REGNET_DIFF)) == "16.0"` (`regnet.py:56` inserts
  `REGNET_DIFF`, surfaced as float via `difficulty.py:81`).

So `"16.0"` vs `"16"` and `"24"` vs `"24.0"` are **genuinely different on disk**, and
a naive "store the number, reformat on read" would corrupt them. There are two
correct answers, gated by the **C0 byte-identity / legacy-on-disk** rules:

1. **Legacy region (`height < fork_height`, and the entire chain while
   `fork_height is None` — mainnet today): NOT migrated into `diff_store` at all.**
   Per C0 "legacy on-disk byte-identity" and "the 23 GB prod ledger is never
   force-rebuilt", the existing `misc` rows stay where they are and are read
   through the existing SQLite path. `diff_store` is **populated for
   `height >= fork_height` only** (same gating as `block_store` / `txid_index`,
   inventory "fork gating"). This is the cheap, safe answer and it is the one that
   unblocks retiring the trio for *new* (post-fork) history.

2. **Where a legacy TEXT value MUST be reproduced from `diff_store`** (e.g. a future
   full-history migration that lifts old `misc` into LMDB), the record carries a
   **trailing variable-length `raw_text` tail** appended after the 28B fixed head:

   ```
   [ 28B fixed record ][ u8 tlen ][ tlen bytes raw ASCII of the original misc.difficulty TEXT ]
   ```

   On read, the legacy `misc.difficulty` value is the **stored `raw_text` bytes
   decoded as ASCII, verbatim** — never `str(difficulty_e10/10**10)`. This makes
   reconstruction a byte-copy (round-trip proof below), preserving `"16.0"`,
   `"24"`, `"118.4732615334"` exactly. The fixed head still drives LWMA/fork-choice
   (numeric), while the tail preserves the historical TEXT identity. For
   post-fork-native rows (answer 1's region) the tail is **omitted** (`tlen = 0`):
   post-fork there is no pre-existing TEXT to match — the canonical value IS
   `difficulty_e10`, and any API surfacing of it is generated from that integer
   (the API already does `int(float(diff_text))` at `apihandler_blocks.py:93-95`,
   so an integer source is strictly safe there).

**Round-trip proof.**
- Forward (write, post-fork-native): `difficulty (Decimal) → difficulty_e10 =
  int(quantize_ten(difficulty) * 10**10)`; `solvetime = max(0, int(ts - prev_ts))`;
  `cumulative_work = prev_cum + work_units(difficulty_e10)`; pack `"<QIQQ"` with the
  u128 as (lo, hi); `tlen = 0`.
- Reverse (read): `difficulty = Decimal(difficulty_e10) / Decimal(10**10)` — equals
  the original `quantize_ten` value bit-for-bit (10dp grid is exactly representable
  in this integer). `cumulative_work = lo | (hi << 64)`.
- Legacy-tail variant: write stores the **exact original `misc.difficulty` string
  bytes**; read returns those bytes decoded — `f(g(x)) == x` is a memcpy, so
  byte-identity is trivially total over all observed forms (`"16.0"`, `"24"`,
  `"50.0"`, `"118.4732615334"`).

This is the same store-compact / reconstruct-exact discipline as C-RECON for
amounts (C4): we never reconstruct a consensus/legacy string from a lossy numeric;
we keep the lossless integer for computation and (where required) the verbatim
bytes for identity.

### D.7 Cross-check (C7) before the consensus-read flip

Per C7 the SQLite→LMDB read flip is **per-projection** and gated on shadow +
`parity_strict` through `storage_backend.cross_check` (`storage_backend.py:130`),
extended here with the `diff_store` invariant:

- For every height `h` in range: `Decimal(diff_store difficulty_e10 / 10**10)` ==
  `Decimal(SELECT difficulty FROM misc WHERE block_height = h)` (numeric equality —
  the `quantize_ten` grid is exact on both sides).
- And, where the legacy-tail is present: `diff_store raw_text` bytes ==
  `(SELECT difficulty FROM misc WHERE block_height = h)` encoded ASCII (**byte**
  equality, catching the `"16.0"` vs `"16"` class).
- `diff_store solvetime[h]` == `int(ts(h) - ts(h-1))` recomputed from the miner-tx
  timestamps `difficulty.py:_recent_solvetimes` reads — so the LWMA window read
  (D.3) provably matches the legacy timestamp-scan input.
- `diff_store cumulative_work[tip]` == `Σ work_units` recomputed independently over
  the range (catches any packing/limb-order bug).

Only after `replay_verify` reports **0 mismatches at `fork_height=None` AND across
a straddling pre+post-fork regnet chain** (C0 "replay-validated") may a
`difficulty_consensus` read flag flip `difficulty.py:70-71` /
`apihandler_blocks.py` from the SQLite `misc` read to the `diff_store` read.

### D.8 Fork gate (C0: one signal, gate by destination height)

There is **no new fork signal**. `diff_store` writes and the LWMA-driven values key
entirely off the existing single `node.fork_height`:

- **Population gate (write site).** In `dbhandler_write.to_db`
  (`dbhandler_write.py:216-219`, the existing `INSERT INTO misc`), alongside the
  legacy insert, write `diff_store.put(hkey(height), record)` **iff**
  `fork_height is not None and height >= fork_height`. While `fork_height is None`
  (mainnet today) this branch is never taken — 100% legacy path, zero behavior
  change (mirrors how `block_store`/`txid_index`/`vm_state` are "post-fork only",
  inventory "fork gating").
- **LWMA value gate (already present, unchanged).** `difficulty.py:106-107`:
  `fh = node.fork_height; if fh is not None and (block_height+1) >= fh:` selects the
  LWMA retarget. `diff_store` only changes *where* `diff_block_previous` and the
  solvetimes are read from; it does not change *whether* LWMA runs. The gate stays
  the one at `difficulty.py:107`.
- **Read gate (consensus).** The `difficulty_consensus` flag (D.7) routes the
  `diff_block_previous` read to `diff_store` only for `height >= fork_height`; below
  the fork, and whenever `fork_height is None`, the read stays on SQLite `misc`.
- `amounts.LEDGER_INTEGER` (C0) is irrelevant here: difficulty is not an amount and
  is never routed through `from_units`. The two flags stay decoupled.

Dispatch/gate sites, file:line:
- write: `dbhandler_write.py:218-219` (`INSERT INTO misc`) — add the gated `diff_store.put`.
- prev-diff read: `difficulty.py:70-71` (`SELECT difficulty FROM misc ... LIMIT 1`).
- LWMA dispatch: `difficulty.py:106-112`.
- API reads: `apihandler_blocks.py:93-95`, `:158-162`.
- rollback: `dbhandler_write.py:50,64,78,84` and `chain_ops.py:721,773` — add
  `diff_store.rollback(keep_height)` (range-delete keys `> keep_height`, the
  `txid_index.rollback` / `reward_chain.rollback` pattern, then re-fold
  `cumulative_work` from the new tip on the next write — or restore the new tip's
  `meta[b"root"]` from a snapshot per C7).

### D.9 How this lets the SQLite `misc` table be retired

With `diff_store` in place and its consensus-read flip validated (D.7):

1. The **only** consensus-relevant `misc` reads — `difficulty.py:70-71`
   (prev difficulty) and the LWMA's `diff_block_previous` — are served from
   `diff_store` for the post-fork region.
2. The **write** at `dbhandler_write.py:218-219` is mirrored into `diff_store`;
   once the SQLite `misc` read is gone, the SQLite `INSERT INTO misc` can be dropped
   from the post-fork write path.
3. The **API** reads (`apihandler_blocks.py:93-95,158-162`) are sourced from
   `diff_store` (integer `difficulty_e10`, which is exactly what
   `int(float(diff_text))` already produces).
4. **Rollback** of difficulty moves to `diff_store.rollback` alongside the existing
   block_store/projection rollbacks.
5. `chain_ops` cross-integrity checks that today compare `misc` max-height vs
   `transactions` max-height (`chain_ops.py:211-225,360-415`,
   `dbhandler_queries.block_height_max_diff` `:148-150`) are replaced by the
   `diff_store` anchor (`meta[b"anchor"]`) vs `block_store` tip.

Because `diff_store` is a rebuildable projection (D.1) it is regenerated from a
**block_store snapshot copy** (C7, never a hot full-scan of the 23GB prod ledger),
self-verifies via its blake2b-32 state-root (D.5), and — per the legacy
byte-identity rule (D.6) — never has to re-derive a historical TEXT string from a
number. With the prev-difficulty, LWMA, fork-choice-work, API, write, and rollback
paths all served by `diff_store`, `misc` is the last consensus-relevant reader of
the SQLite trio for the post-fork region, and removing it clears the final blocker
to deleting H1/H2/H3 for new history.


**Savings**

| field | legacy form | legacy bytes | true-bytes form | new bytes | saving % |
|---|---|---|---|---|---|
| difficulty | `misc.difficulty` TEXT, `str(float)` e.g. `"118.4732615334"` | ~14 (ASCII, var 3–18) | `difficulty_e10` u64 LE | 8 | ~43% |
| solvetime | derived: `SELECT timestamp ... reward!=0` scan + subtraction (not stored) | n/a (recomputed per retarget) | `solvetime` u32 LE | 4 | new field (removes the per-retarget timestamp scan) |
| cumulative work | does not exist anywhere (fork-choice is height-only today) | 0 (absent) | `cumulative_work` u128 LE (2×u64 limbs) | 16 | new capability (enables most-work fork choice) |
| per-row key | SQLite `block_height INTEGER` + row/btree overhead | ~8 + index | `Codec.hkey` 8B BE uint64 | 8 | parity (shared C1 key) |
| **post-fork-native row total** | misc row (TEXT diff + height + sqlite overhead) ~30–40 incl. `idx_misc_block_height` | ~30–40 | 28B fixed record (`<QIQQ`), no tail | 28 | ~20–30% **and** adds solvetime + cumulative work that legacy never stored |
| legacy-history row (if ever migrated) | misc TEXT row | ~14 (value) | 28B head + `u8 tlen` + verbatim TEXT tail | 28 + 1 + len | larger by design — buys byte-exact TEXT reconstruction (D.6) plus the two new fields |


**Adversarial fixes folded in:**
- First-pass completeness gap (the whole reason for this section): the SQLite misc(block_height, difficulty) table had NO LMDB home, blocking retirement of the trio. Fixed by adding the diff_store projection env with a 28B fixed record giving misc.difficulty an LMDB home, plus solvetime and the previously-nonexistent cumulative_work.
- Grounded every claim in real source rather than assertion: verified diff_save = float('%.10f' % difficulty) (difficulty.py:139) is bound to a TEXT col and read back as a string ('16.0', '24'), and that the except-fallback yields int 24 -> '24' (not '24.0') — this drove the verbatim raw_text tail in D.6 so legacy TEXT byte-identity is a memcpy, not a lossy reformat.
- Honored C1 keyspace-aliasing: diff keys are real positive heights only (the misc table never had negative heights; only transactions does), so diff_store lives in its own env and can never alias reward_chain's negated keys.
- Honored C2 (pinned blake2b digest_size=32 for the state-root, vm_state.py:97 template) and C4 width discipline (cumulative_work is unbounded-growing -> u128, not u64, explicitly justified as the same class as token amounts).
- Honored C6/C7/C8: diff_store is a rebuildable projection in its own env, raw-bytes no-codec record (A-hex ban), carries meta[b'fmt']=\x04 sentinel and a sorted-kv blake2b-32 state-root, rebuilt from a block_store snapshot (never a hot full-scan of the 23GB prod ledger per the no-heavy-scans memory).
- Honored C0 determinism: defined work_units only in the Decimal domain under fixed localcontext prec with Decimal.exp/ln (correctly-rounded) and ROUND_FLOOR, mirroring difficulty_lwma.py:18-22's exact libm-vs-Decimal hazard, so cumulative_work is bit-identical across platforms.
- One-fork-signal compliance: all gates read node.fork_height; population gate at the existing write site; LWMA gate unchanged at difficulty.py:107; consensus-read flip gated per-projection via replay_verify 0-mismatch at fork_height=None AND a straddling chain (C7). amounts.LEDGER_INTEGER explicitly kept decoupled.


**Implementation checklist (residual review items to satisfy before wiring this domain):**
- [ ] Fix the false API claim. Do NOT assert all difficulty API reads are int(float(...)). Enumerate the ACTUAL surfaces and their exact output form: (a) getblockfromhashextra (apihandler_blocks.py:93-95) -> int(float(diff_text)), integer-safe; (b) getblockrange (apihandler_blocks.py:158-162 -> block_format.blocktojsondiffs:74, block_format.py) -> raw TEXT string list_of_diffs[i][0]; (c) /api/stats/difficulty (rest_stats.py:497) -> raw TEXT; (d) ledger_explorer.py:138 -> raw TEXT. For every surface that emits the raw string, the post-fork wire form MUST be reproduced byte-identically.
- [ ] Make post-fork-native reconstruction of the exact str(diff_save) form possible. Either (preferred) ALWAYS store the verbatim raw_text tail (the exact str(diff_save) bytes that to_db/dbhandler_write.py:218-219 binds) for EVERY diff_store row including post-fork-native (drop the tlen=0 'no tail post-fork' optimization), so getblockrange/stats/explorer reconstruct '16.0'/'118.4732615334' by memcpy; OR pin a canonical reconstruction rule reproducing str(float('%.10f' % (difficulty_e10/1e10))) exactly AND add a byte-equality cross-check (D.7) for the string form on the post-fork region too (not only numeric equality). Numeric-only cross-check will not catch '16.0' vs '16'.
- [ ] Add dbhandler_queries.py:56 (difflast) to the prev-difficulty read-flip inventory in D.8/D.9 and route it to diff_store under the same difficulty_consensus gate; document that difflast feeds the REST /api/difficulty descriptor and rest_client.py:64-78's cross-node divergence reference, so its reconstructed numeric/string form must match the SQLite form during the shadow window.
- [ ] Extend the D.7 cross-check to assert the API/explorer string output byte-for-byte (getblockrange JSON difficulty field, /api/stats/difficulty samples) between the SQLite-sourced and diff_store-sourced paths over a straddling chain, before any API read flips off SQLite -- mirroring storage_backend.cross_check's byte-equality (==) discipline rather than numeric equality.
- [ ] Reconcile/justify the two Decimal precisions (work_units _WORK_PREC=80 vs difficulty_lwma _PRECISION=50) explicitly, or reuse a single pinned constant, so a later 'cleanup' cannot silently change the cumulative_work grid.

---

# Completeness & cross-domain consistency

**Gaps the first-pass critic surfaced (now owned by a domain):**
- misc / difficulty table (CREATE TABLE misc (block_height INTEGER, difficulty TEXT) — genesis/regnet.py:43, migrate_amounts.py:29). doc/26:179 EXPLICITLY flags it as a gap: 'hashextra also read the misc difficulty table — not in the block store'. No design domain owns per-block difficulty/cumulative-work storage. block-header domain covers hashes/txid list but the difficulty column is a separate SQLite table that has NO LMDB home in any of the 8 stores. Post-fork LWMA (difficulty_lwma.py) needs recent solvetimes+prev_diff, but block_store value {'h':hash,'t':[txs]} stores neither difficulty nor a usable per-block timestamp-for-retarget; this is unowned.
- Negative-height reward mirror ROWS as stored in legacy SQLite (the raw dev-fund/hypernode payout rows with block_height < 0). The plugin-stores/reward_chain domain covers the SIDECHAIN projection (reward_chain.py negates the height to a POSITIVE key via Codec.hkey), but block_store.py keys strictly by _hk = struct.pack('>Q', height) (unsigned big-endian uint64) and CANNOT represent a negative height. So the canonical block_store has no row for the original negative-height entries — the design assumes reward_chain fully subsumes them, but no domain states the invariant that block_store is positive-height-only and that ALL negative-height legacy rows are losslessly reconstructable from reward_chain. This boundary is undocumented in the domain set.
- Mempool true-bytes storage. 'mempool' IS listed as a design domain, but the inventory's legacy-sql section shows mempool is still a standalone SQLite table (mempool_sql.py:30-32, 9 columns incl. mergedts INTEGER(4)). No LMDB schema, key encoding, or true-bytes layout was actually designed for it — the domain is named but the inventory shows zero KV/LMDB mempool store exists. The mergedts server-side default (strftime('%s','now')) and the substr(signature,1,4) prefix-index access pattern (mempool.py:425) have no KV equivalent designed.
- block_store value-level fields fee and reward per stored tx. The 11-field stored tx in blocks[height].t[] retains fee/reward as msgpack-packed values, but no domain specifies their true-bytes encoding (integer units vs '%.8f' string vs msgpack int). tx-fields domain lists fee/reward conceptually but the on-disk packing inside block_store's msgpack record is left to Codec.pack defaults; whether they are integer atomic units post-fork is unspecified.
- Cumulative chain work / total-difficulty (used for fork-choice / heaviest-chain). Neither block-header nor core-indexes domain covers a per-height cumulative-work index; today this is derived by scanning misc.difficulty. Post-SQLite this has no store.
- block_hash field TYPE drift inside block_store: stored as hex STRING in the msgpack value ({'h': block_hash}) while txid_index/shielded use bytes/hex and block_hash_v2 emits 64-hex. No domain pins whether post-fork block_store should store the 32 raw hash bytes vs 64-hex string (2x bloat); block-header domain designed the hash algorithm but not its at-rest byte form in block_store.

**Cross-domain risks reconciled by the shared conventions (C0-C8):**
- Height key encoding (Codec.hkey = struct.pack('>Q', height), 8-byte big-endian uint64) is shared by block_store.blocks/hashes, reward_chain.rewards, token_index (cred/deb/journal/alias_rev/ajournal tails), shieldedv1 (notes_h/kimg_h/flows), and txid_index VALUES. ALL of these must agree byte-for-byte on big-endian-for-lexicographic-ordering AND on uint64 (positive-only). The reward_chain domain RE-USES the same positive key space by negating heights — if block_store ever stored a real height that collides numerically with a negated reward height, two domains would alias. The invariant 'reward_chain heights are negated-from-negative, block_store heights are real-positive, they never share an environment' is implicit and must be stated, or a cross-domain key collision is possible.
- txid encoding disagreement across domains. token_index.seen stores txid as txid.encode() (hex/sig STRING, ~56-64B), txid_index stores txid.encode() OR raw bytes as KEY, the A.1 canonical content-txid (doc/18) is 64-hex lowercase blake2b, and legacy dedup uses signature[:56]. The core-indexes (txid_index) and plugin-stores (token_index.seen) and signatures domains must agree on ONE txid byte form post-fork. If txid_index keys raw 32 bytes but token_index.seen keys 64-hex, the same logical txid has two on-disk forms — any cross-check or shared lookup breaks. doc/29 sub-optimal-finding #1 already flags token_index.seen hex bloat.
- blake2b digest_size / output width must agree between block-header (block_hash_v2 = blake2b-256, 64-hex), tx-fields/txid (tx_id_v2 = blake2b-256, 64-hex), pubkey storage (block_store.pk dedup = blake2b digest_size=32), and PoW (Heavy3 inner sha224->blake2b at 28 bytes per doc/18 §D). Two DIFFERENT blake2b widths are in play (32-byte for hashes/dedup, 28-byte for PoW inner). The PoW domain (not in the listed set but referenced by coinbase/block-header) uses 28-byte blake2b while consensus hashing uses 32-byte — any spec that says 'blake2b' without pinning digest_size risks a domain using the wrong width. Pin digest_size everywhere.
- Amount integer-units convention must be identical across tx-fields (block_store stored tx amount/fee/reward), plugin-stores (token_index cred/deb as str(amount), reward_chain amount_units, shieldedv1 flows/notes amt), balance_index ([credit,debit] units), and vm (balances/storage 32-byte uint256 units). amounts.to_units = 1 BIS = 100_000_000. If any domain stores BIS-Decimal while another stores atomic units, balance_index parity-assert vs ledger_balance3 (doc/26:186) silently diverges. The consensus_amount/from_units reconstruction discipline (amounts.py:65) must be referenced by EVERY domain that round-trips an amount to a consensus pre-image.
- Public-key by-reference: block_store already losslessly dedupes pubkeys via pk/pkr (blake2b(pk)->id, id->raw pk). The signatures + pubkeys domains independently considered a consensus address->key registry (REJECTED in doc/29 §2.C). RISK: if the pubkeys domain re-introduces any pubkey-dropping in the tx record (pubref=1) that depends on block_store.pkr being populated in a SPECIFIC order, it recreates the intra-block ordering dependency that doc/29 decisively rejected. The two domains must agree that pubkey compaction is STORAGE-LAYER (block_store dedup, transparent) and NOT consensus-tx-record, or they conflict.
- Address byte form. The address domain (base58 -> raw) interacts with block_store (pk dedup keyed by blake2b(pubkey), value rows store address as recovered/stored), balance_index (key = address.encode()), token_index (addrtok/cred/deb embed address), shieldedv1, vm_state (contract addr = address.encode()), and signerfactory regex dispatch (which matches on the STRING form Bis1.../Bism...). If address moves to raw bytes in one domain (e.g. balance_index key) but signerfactory still pattern-matches the base58 string, the same account has two key forms across stores and the regex-based signer dispatch (signerfactory.py:120-134) breaks. Address raw-byte migration must be all-or-nothing across every store that keys by address.
- JSON vs msgpack codec split. token_index (tokreg/journal/alias_fwd/ajournal) and shieldedv1 (notes) deliberately use json.dumps(separators=(',',':')) for byte-parity with the legacy store, while block_store/balance_index/reward_chain use Codec.pack (msgpack). A true-bytes spec that says 'use the Codec everywhere' would BREAK the byte-identity tests (test_lmdb_on_disk_bytes_identical, doc/36:269) for the JSON stores. The plugin-stores and core-store domains must explicitly agree which stores are msgpack and which are frozen-JSON; this is a real cross-domain trap during migration.

**Storage-engine recommendations (carried into the design):**
- ML-DSA signature compression: ML-DSA-87 sigs are 4,627 raw bytes (1,312 / 3,309 / 4,627 for 44/65/87) and recur per-address. block_store ALREADY dedupes PUBKEYS via pk/pkr but NOT signatures (each sig is unique, incompressible by dedup). Recommend: (a) store sigs as RAW bytes not base64 (doc/29 already plans this; saves 33%), (b) DO NOT attempt entropy compression of lattice sigs — ML-DSA sigs are high-entropy and gzip/zstd yields <2%, not worth the CPU; (c) instead pursue ML-DSA pubkey-by-reference at the STORAGE layer ONLY (extend block_store.pk/pkr to cover ML-DSA pubkeys, which ARE 1184/1952/2592 bytes and DO recur), keeping the consensus tx record carrying raw sig but pubref=1 — this matches the doc/29 rejection (no consensus registry) while still reclaiming the big pubkeys via the existing transparent dedup table.
- Append-only + free-list layout: LMDB is already copy-on-write B+tree with its own free-list (reclaims pages from aborted/old txns via MDB_NOTLS + writemap tuning). Recommend explicitly: keep block_store as the ONLY append-heavy env, set a generous map_size headroom (23GB prod ledger -> size for 100GB+), and run periodic mdb_copy --compact for offline compaction rather than relying on in-place reuse. Keep projections (balance_index, txid_index, token_index, shielded, reward_chain, vm_state) in SEPARATE envs so a projection rebuild (drop+rebuild) never fragments the canonical block_store. Per the no-heavy-scans-on-prod-ledger memory: NEVER full-scan block_store to rebuild a projection while prod is live; rebuild from a snapshot copy.
- Snapshot format: define a single canonical snapshot as the set of LMDB envs + a manifest {tip_height, tip_hash, fork_height, per-env state-root}. The vm_state already has a blake2b-32 state root (vm_state.py:97); add an analogous per-env root (sorted-kv blake2b) for balance_index/token_index/shielded so a downloaded snapshot is self-verifying against the block at tip. Snapshot should be the LMDB files themselves (mdb_copy --compact), NOT a re-serialized dump — this preserves the proven byte-identical on-disk format (doc/36:269) and makes snapshot restore O(copy).
- cross_check strategy during migration: extend storage_backend.cross_check (currently get_block byte-equality, storage_backend.py:130) to ALSO assert per-projection: balance_index vs ledger_balance3 (already shadow+parity_strict, doc/26:186), txid_index vs signature-scan dedup (doc/26:203), AND add reward_chain vs negative-height-row sum, token_index cred/deb sums vs SQLite SUM/GROUP-BY, shielded pool vs ledger_balance(SHIELD_SINK) (the doc/22 supply invariant). Run all of these in shadow mode over a STRADDLING regnet chain (pre+post fork) with replay_verify.py reporting 0 mismatches, per the doc/29 invariant, before flipping any *_consensus flag to primary. Gate the flip per-projection, not all-at-once.
- Pin every blake2b digest_size in the spec table: hashes/txid/block-hash = 32 (64-hex), block_store pubkey dedup = 32, PoW Heavy3 inner = 28. State them explicitly so no domain defaults to the wrong width.
- Store block_hash as RAW 32 bytes in block_store.blocks value (not 64-hex string) post-fork — halves the per-block hash overhead and matches the bytes that block_hash_v2 actually digests; provide hex only at the API boundary. Apply the same raw-bytes rule to token_index.seen txids and shielded note_id/kimg (doc/29 sub-optimal findings #1-3: hex->raw is a 2x win on high-cardinality keys).
- Replace string-encoded integers in token_index (cred/deb/addrtok/tokset/meta/flows) and shielded (flows/meta) with fixed big-endian uint64/varint where the value is a pure counter or amount — but ONLY for NEW post-fork stores, and ONLY if the byte-identity test is re-baselined, since changing these breaks the current 'byte-identical to pre-migration' guarantee (doc/36:269). Flag this as a deliberate format break gated on fork_height, with its own characterization test.
- Add an explicit difficulty/work store to the spec (the missing misc table): a sub-db diff: hkey(height) -> packed (difficulty, solvetime, cumulative_work). LWMA reads the last N via a range scan; fork-choice reads cumulative_work at tip. This closes the doc/26:179 gap and removes the last consensus-relevant SQLite read (misc.difficulty) so the SQLite trio can actually be deleted post-fork.

**Final consistency check (after corrections):** all_gaps_closed=True, cross_domain_consistency_ok=True.

Remaining minor items:
- Mempool true-bytes schema is named in the SHARED CONVENTIONS domain-list preamble and in the original-critic-gap list, but no C-clause (C0-C8) actually pins a mempool on-disk encoding. This is defensible (mempool is ephemeral/non-consensus and owned by its own domain spec mempool.py), so it is not a true cross-domain gap — but the convention section asserts coverage of 'mempool' as one of the domains that 'MUST obey it verbatim' while giving mempool no shared bytes to obey. Either drop mempool from the spine's enumerated domain list or add a one-line C-clause noting mempool is intentionally exempt (ephemeral, no byte-identity contract).
- C3 says 'store the raw 32 bytes' for the canonical txid, but the cited producer tx_id_v2 / tx_id_at (bismuth_serialize.py:96-98,166-175) returns a 64-hex hexdigest() string, not bytes. The spec does call this out (hex at API/legacy, raw-32 in keys), so a consumer MUST bytes.fromhex() the producer's output before keying. This is correctly specified but is an implicit conversion step every keying store must perform; worth an explicit C-RECON-style note that the raw-32 key is bytes.fromhex(tx_id_at(...)) so no domain stores the hex string by mistake.

Convention citation fixes to apply:
- C0 citation error: 'amounts.LEDGER_INTEGER (amounts.py:65)' is wrong. In the actual file LEDGER_INTEGER is defined at amounts.py:20; line 65 is consensus_amount(). The C4 citation 'amounts.consensus_amount ... amounts.py:65-72' IS correct, so the line number 65 was copied onto the wrong symbol in C0. Fix C0 to cite amounts.py:20 for LEDGER_INTEGER.
- Minor citation drift (non-blocking, substance correct): C0/C3 cite the v2 txid gate as 'digest.py:105-107', but digest.py:105-107 is reward_chain-adjacent count code in this tree; the real tx_id_at dispatch in the digester is around digest.py:624-626 (verified) and the per-tx fork gate is digest_tx.py:113-114 (verified). The block_hash_v2/tx_id dispatch and fork_height set/persist blocks were confirmed present, just a few lines off from the cited ranges (e.g. fork-lockin/save_locked_height verified in the digest.py:540-560 region matching the cited 542-557).

Verified the load-bearing source citations against /root/bismuth-claude/Bismuth/. The shared-convention spine is sound and the corrected domains are mutually consistent on every cross-domain seam the critic flagged.\n\nCONFIRMED CORRECT (byte-for-byte against source):\n- C1 Codec.hkey = struct.pack(\">Q\", ...) big-endian uint64 (kvstore.py:74-75; unhkey 78-79). Reused identically by token_index struct.pack(\">QQ\",height,seq) (token_index.py:67), txid_index value h.to_bytes(8,\"big\") (txid_index.py:77,96), block_store.hashes value _hk(height) (block_store.py:115).\n- C1 negative-height invariant: reward_chain.extract_from_ledger selects block_height<0 and stores add(-int(r[0]),...) i.e. NEGATES to a positive hkey (reward_chain.py:97-98, _hk=Codec.hkey line 38). block_store is positive-real-height only. Separate-env requirement is the correct guard against numeric aliasing — sound.\n- C2 blake2b widths: block_hash_v2 / tx_id_v2 digest_size=32 (bismuth_serialize.py:97-98,147); block_store pubkey-dedup blake2b digest_size=32 (block_store.py:79); vm_state state_root blake2b digest_size=32 over sorted code+storage+balances (vm_state.py:104-119). 28-byte Heavy3 inner is correctly carved out as the sole exception.\n- C3 single txid form: tx_id_v2 over the binary v2 pre-image, dispatched by destination height via tx_id_at (bismuth_serialize.py:166-175). Correctly notes token_index.seen and shieldedv1 note_id/key_image are HEX TEXT today (token_index.py:135,167 txid.encode(); shieldedv1 note_id .hex()) and that v2 makes them raw — a deliberate fork-gated break, not a silent flip. Legacy signature[:56] dedup boundary correctly preserved.\n- C4 amounts: 1 BIS = 1e8 atomic units (amounts.SATOSHIS_PER_BIS). C-RECON discipline is the right call: consensus_amount (amounts.py:65-72, = from_units in integer mode) for any value re-entering a consensus pre-image, and display_amount (amounts.py:55-62) is explicitly float/lossy >2**53 and banned from consensus — verified verbatim in the source docstrings, which themselves warn this forks socket-vs-REST sync. The bounded-BIS-u64 vs unbounded-token-varint/u128 rule is correct and important.\n- C5 address all-or-nothing + storage-raw/dispatch-string: SignerFactory.address_to_signer routes by regex on the base58 STRING (signerfactory.py:122-134), so the storage-raw / dispatch-string split is mandatory and correctly stated.\n- C6 Codec split: block_store value really is msgpack {\"h\":block_hash,\"t\":txs} (block_store.py:114); hashes/txid values raw >Q; token_index/shielded JSON-valued stores stay frozen-json for byte-parity. Consistent with source.\n- C7 cross_check is the real proof harness (storage_backend.py:130-142, asserts get_block rows AND block_hash==rows[0][7]); per-env state-root templated on vm_state.state_root. Per-projection gated flip is consistent with C1's separate-env mandate.\n- C8 format-version sentinel is a net-new safety addition with no conflicting source; sound rationale (silent-miss on hex->raw key probing).\n\nNo contradictions found between any two C-clauses, and the convention list the orchestrator asked to reconcile (hkey keying, single txid form, pinned blake2b widths, amount-units, address all-or-nothing, json/msgpack split) is internally consistent and matches code. The two citation errors above are documentation defects, not design inconsistencies — they do not change any byte on disk. all_gaps_closed=true because every enumerated critic gap is substantively addressed; the two remaining_gaps are spec-hygiene items (mempool exemption wording, explicit hex->raw-32 conversion note), not open cross-domain risks.

---

# Validation gates (before any consensus-read flip)

Per C0 (replay-validated) and the no-heavy-scans rule, NO per-domain wiring flips a `*_consensus` read to primary until ALL of the following pass, per projection, gated independently:

1. **Round-trip self-check** `encode(decode(x)) == x` across a FULL mainnet replay from a **snapshot copy** (read-only; never a hot full-scan of the live 23 GB prod ledger). Acceptance: ZERO rows fall into an opaque/verbatim fallback for a scheme that should pack cleanly.
2. **`replay_verify` 0 mismatches** at `fork_height=None` AND across a straddling (pre+post-fork) regnet chain.
3. **`storage_backend.cross_check` / `block_store.verify_against_sqlite`** byte-for-byte on the reconstructed forms across the boundary block.
4. **Characterization vectors** re-baselined to the new true-bytes form (the deliberate fork-gated format break), including every edge row: zero amount/fee/reward, coinbase, `genesis`/non-address recipients, negative-height reward mirrors, non-canonical base64, leading-`1` base58.

The codecs implemented this commit (`sigbytes`, `addrbytes`) already satisfy gate (1) in isolation (26 passing round-trip tests, all tags incl. fallbacks); gates (2)-(4) attach when wired into `block_store` and exercised on a straddling regnet chain.

