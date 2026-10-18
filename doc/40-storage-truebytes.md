# doc/40 - hf2 Stage-4: true-bytes LMDB storage

Status: **implemented and multinode-validated.** This is Stage 4 of the hf2 serialization rework
([doc/29](29-hf2-serialization-v2.md)): every post-fork field the ledger stores is kept as **true bytes**
(raw digests, raw signatures, varint integers, tagged address payloads) instead of base64 / hex / decimal
text, gated on the single `node.fork_height` by **destination height**. Pre-fork rows keep their legacy
string form; with `fork_height is None` (mainnet until on-chain lock-in) the true-bytes paths never run.

The ledger is ONE LMDB environment, `blockstore-<ledger name>` (e.g. `static/blockstore-ledger.db`,
`kvstore.store_path`), owned by `block_store.BlockStore` ([doc/05](05-database-and-ledger.md)). Everything
in this document lives in that env, except the optional token/alias plugin store (§9).

## Implemented compaction stages

| # | Stage | What it compacts | Module(s) | Saving | Commit |
|---|---|---|---|---|---|
| 1 | **Signature true-bytes** | per-scheme base64 / RSA single-b64 / recoverable-hex → raw bytes + 1-byte tag; `0x00` opaque fallback | `sigbytes.py` | ~24–48% / sig | `55fd4b40` |
| 2 | **Address/recipient true-bytes** | base58 / 56-hex → raw tagged union; `0xFF` verbatim fallback (genesis/sinks) | `addrbytes.py` | ~23–47% / addr | `55fd4b40` |
| 3 | **Block-body codec wiring** | sig + address packed on write, rebuilt in `_expand` on read, fork-gated by destination height | `block_store.py` | — | `51a82f74` |
| 4 | **tx-fields true-bytes** | timestamp → varint centiseconds; amount/fee/reward → varint integer units | `txfields.py` | ~50–60% / field | `80e47241` |
| 5 | **Difficulty record** | per-block difficulty kept in the ledger's `diff` sub-db, written in the same transaction as the block | `block_store.py` | — | `948958a8` |
| 6 | **Core index keys** | `hashes` keyed by the raw digest; `txid` keyed by the raw 32-byte content txid; `bal` as fixed-width little-endian integers | `block_store.py` | ~50% on index keys | `948958a8` |
| 7 | **Binary sync transport** | `/api/blocks/range?format=binary`: JSON+hex sync payload → raw-byte binary (negotiated, JSON fallback) | `sync_codec.py`, `rest_api.py`, `rest_client.py` | ~0.6–0.72× JSON | `1e29d4e7` |
| 8 | **Block-hash raw bytes** | envelope `"h"` → raw 32 B (was 64-hex), rebuilt to hex on read | `block_store.py` | 32 B / block | `4bf73ef2` |
| 9 | **txrec consolidation** | the 11-field msgpack list → ONE concatenated blob per tx (no per-element array framing) | `txrec.py`, `block_store.py` | ~20 B / tx | `b6dfddaa` |
| 10 | **Block-hash hoist** | per-tx `block_hash` not stored — filled from the envelope hash on read | `txrec.py`, `block_store.py` | ~33 B / tx | `28fc87d2` |
| 11 | **Address-ref dedup** | address + recipient → varint indices into a per-block address dict; a repeat costs ~1 B not ~30 B | `txrec.py`, `block_store.py` | ~30 B / repeat | `ec71d163` |
| 12 | **Pubkey-by-reference** (consensus) | RSA / ML-DSA / multisig pubkey dropped from wire + pre-image for repeat senders, resolved by address from the `pkref` sub-db | `block_store.py`, `polysign/signerfactory.py` | ~0.5–2.6 KB / repeat send | `87bb4faa` |
| 13 | **Coinbase mining header + multi-scheme mining** (consensus) | coinbase is PoW-authorized, never signature-verified; its freed sig/pubkey slots carry the mining header ([doc/41](41-hf2-coinbase-free-fields.md)); any scheme mines post-fork | `bismuth_serialize.py`, `digest.py`, `digest_tx.py`, miner/pool/regnet | ~736 B–2.6 KB / block | `f7826513` |

**Net effect on a post-fork tx record:** one `txrec` blob carrying a varint timestamp, two varint address
indices into the per-block dict, varint integer amount/fee/reward, a raw per-scheme signature, a varint
pubkey dedup id, and length-prefixed operation/openfield. The block hash and addresses are hoisted to the
block envelope; pubkeys are stored once.

**Not part of this stage:**
- **Wire protocol.** The consensus tx is still the 8-field string tuple on the socket wire (hence the
  `'%.8f'` / `str()` conversions in digest/miner/pool). The hash/sign pre-image, storage and the REST sync
  transport are binary; a typed wire object is its own consensus cycle.
- **Mempool.** The mempool is an in-memory dict (`mempool.py`); it has no store to compact.

---

## 1. Governing invariants

- **One fork signal.** Every consensus gate reads `node.fork_height` and nothing else.
  `amounts.LEDGER_INTEGER` is a **storage** flag: it changes how a money field is stored and rebuilt,
  never whether a height is post-fork.
- **Gate by destination height.** A block is true-bytes iff its own height `>= fork_height`. There is no
  process-wide "v2 mode". A store holding a straddling chain carries legacy records below the fork and
  true-bytes records at and above it; the reader dispatches per element (`bytes` = packed, `str`/`list` =
  legacy).
- **Frozen pre-images.** `signature_buffer` / `tx_id` / `block_hash` and their v2 siblings
  `signature_buffer_v2` / `tx_id_v2` / `block_hash_v2` / `_v2_tx_bytes` (`bismuth_serialize.py`) are never
  touched by storage. Storage keeps the compact form; **reconstruction rebuilds the exact wire strings**
  those functions consume.
- **A-hex ban.** "Raw" means true bytes in an LMDB key or value, never hex-in-text (hex is 2×, base64
  1.33×, raw 1×).
- **Consensus equivalence is the correctness bar.** A reconstructed post-fork row re-digests to the same
  block hash and content txid it was accepted with; `replay_verify` recomputes them from stored rows and
  must report 0 mismatches over a straddling chain.

## 2. Shared conventions

### 2.1 Height keys

- `Codec.hkey(h) = struct.pack(">Q", h)` — 8-byte big-endian u64 (`kvstore.py`). Used for `blocks`,
  `diff`, `pkr` keys and for the `hashes` / `txid` / `pkref` height values. Big-endian makes byte order
  equal numeric order, so tips, rollbacks and windows are ordered range scans with no comparator.
- Index keys that embed a position (`sig`, `addr`) use **s64**: the height offset by 2⁶³
  (`block_store._sh`), followed by a big-endian u16 in-block index.

### 2.2 blake2b widths

A bare "blake2b" is never written; the width is pinned per use.

| Use | `digest_size` | Boundary form |
|---|---|---|
| Post-fork block hash (`block_hash_v2`) | **32** | 64-hex |
| Content txid (`tx_id_v2`) | **32** | 64-hex |
| Pubkey dedup key (`pk`) | **32** | raw 32 B key |
| PoW Heavy3 inner | **28** | — ([doc/18](18-hardfork-hf2.md)) |

### 2.3 txid byte form

Post-fork there is exactly one txid: `tx_id_v2`, blake2b-256 of the v2 signature pre-image, dispatched by
destination height through `bismuth_serialize.tx_id_at`. It is stored as the **raw 32 bytes** (`txid`
sub-db key); 64-hex lowercase exists only at the API boundary. Pre-fork, duplicate detection is
`signature[:56]` (the `sig` sub-db); the content txid is the dedup identity only at `height >= fork_height`.
Any cross-boundary comparison uses `signature[:56]` below the fork and the raw content txid at/above it.

### 2.4 Amount units

All amounts are integer atomic units, `1 BIS = 100_000_000` (`amounts.to_units` / `from_units`). Any value
that round-trips into a consensus pre-image or a sync wire form goes through `amounts.consensus_amount`
(= `from_units` in integer mode) — never the float `display_amount`, which loses precision above 2⁵³ and
would split socket vs REST sync. BIS amounts are bounded (capped supply); token amounts are unbounded and
must never be packed into a u64.

### 2.5 Address form: storage-raw, dispatch-string

`SignerFactory` routes by regex on the base58/hex **string** (`^Bis1…$`, `^Bism|mBis…$`, `^[abcdef0-9]{56}$`,
versioned prefixes). Storage may hold the raw payload, but every address handed to consensus or dispatch is
reconstructed losslessly to its exact string first.

Index keys (`addr`, `bal`, `pkref`) use the address **string's UTF-8 bytes** (capped at 255) — a single
on-disk identity per account across every sub-db. The raw tagged form (`addrbytes`) is used only inside
block bodies, where it is never a lookup key.

### 2.6 Codec split

| Codec | Where |
|---|---|
| **msgpack** (`Codec.pack`, `use_bin_type=True`) | `blocks` values (the block envelope), `meta[b"alloc"]` |
| **Raw bytes, no codec** | `hashes`, `txid`, `pkref`, `pk`/`pkr`, `bal`, key-only `sig`/`addr`, `meta` counters |
| **Text** | `diff` values (the difficulty exactly as the PoW check saw it) |
| **Frozen JSON** (`json.dumps(..., separators=(",", ":"))`) | the token/alias plugin store's JSON-valued sub-dbs (§9) |

True-bytes block bodies require the msgpack codec; `_put_body` refuses a post-fork write without it
(`str` vs `bin` must be unambiguous on unpack).

---

## 3. Signature codec (`sigbytes.py`)

The `signature` field is base64 for RSA / ECDSA-DER / ED25519 / secp256r1 / ML-DSA / multisig, and
lowercase hex for the hf2 recoverable secp256k1 form. Post-fork it is stored decoded:

```
u8 tag || u16le len || raw
```

| Tag | Scheme | Wire envelope |
|---|---|---|
| `0x00` | opaque fallback | verbatim UTF-8 |
| `0x01` | RSA PKCS#1 v1.5 | **single** base64 (128 B raw for 1024-bit keys, 512 B for 4096-bit) |
| `0x02` | secp256k1 legacy DER | base64 |
| `0x03` | ED25519 | base64 |
| `0x04` | secp256r1 / P-256 DER | base64 |
| `0x05` / `0x06` / `0x07` | ML-DSA-44 / -65 / -87 | base64 |
| `0x08` | native M-of-N multisig blob | base64 |
| `0x09` / `0x0A` | BTC / CRW interop | base64 |
| `0x40` | hf2 recoverable secp256k1 `r‖s‖v` (65 B) | lowercase hex |

**Write (`pack_from_wire`).** The tag is derived from the sender address with the authoritative
`polysign.signerfactory` regexes (single-sig secp256k1 is split into `0x40` vs `0x02` by the 130-char hex
shape). The codec then **proves** `encode(decode(s)) == s`; any non-canonical or unclassifiable string is
stored under `0x00` verbatim. Losslessness never depends on classification being right — only the size win
does.

**Read (`to_wire`).** Branches on the stored tag alone and re-applies the envelope. Reversibility holds
because each decode is a bijection on canonical strings: `b64encode(b64decode(x)) == x` for canonical padded
base64, `bytes.fromhex(x).hex() == x` for lowercase even-length hex.

The u16 length makes the blob self-delimiting (largest real signature: ML-DSA-87, ~4.6 KB). The signature is
consumed by `_v2_tx_bytes` as the reconstructed string with its own length prefix, so the block hash is
unperturbed.

## 4. Address / recipient codec (`addrbytes.py`)

The field is **not always an address**: it is truncated to 56 chars and carries sentinels (`"genesis"`,
`"Hypernode"`, 56-hex sinks) that must survive byte-for-byte. Hence a tagged union with a proven round-trip:

| Tag | Family | Body |
|---|---|---|
| `0x00` | RSA / 56-hex / 56-hex sink | `bytes.fromhex(addr)` (lowercase-canonical guard) |
| `0x01` | secp256k1 `Bis1…` (short) | `b58decode(addr)` |
| `0x02` | ED25519 `Bis1…` (long) | `b58decode(addr)` |
| `0x03` | versioned (ML-DSA / secp256r1) | `b58decode(addr)` |
| `0x04` | native multisig `Bism`/`mBis` | `b58decode(addr)` |
| `0xFF` | anything else | `u8 len || raw UTF-8` |

A structured tag is committed only after `b58encode(b58decode(s)) == s` (or the hex equivalent) — this
preserves leading `1`s and rejects non-canonical encodings. Everything else lands in `0xFF`; that branch is
load-bearing for correctness, not a size knob. `unpack_addr` returns a `str` input unchanged (the legacy
transition guard) and raises on a torn verbatim length.

## 5. Numeric fields (`txfields.py`)

- **Varint:** unsigned LEB128, minimal length; the decoder rejects overlong encodings (two byte strings can
  never decode to one value) and truncation (clean `ValueError`).
- **Timestamp:** the `'%.2f'` string ↔ varint of integer centiseconds (the same quantization as the
  consensus `_v2_ts_cs`); rebuilt with integer formatting, byte-identical, no float.
- **amount / fee / reward:** varint integer units. Decimal storage mode packs via `to_units` and rebuilds
  the `'%.8f'` form; integer mode (`amounts.LEDGER_INTEGER`) packs the stored units string with `int` and
  rebuilds `str(units)`.

**Why units and not text.** `block_hash_v2` and `tx_id_v2_s` funnel every amount through `_v2_units`, so
`'0'` and `'0.00000000'` give the identical block hash and txid, and the post-fork signature signs the
content txid. Storage therefore needs consensus equivalence, not the legacy text form: `'0'` rebuilds as
`'0.00000000'` in decimal mode — a deliberate, consensus-safe normalization. `to_units(rebuilt) == units`
in either mode, so a re-digest is byte-identical.

## 6. Block record (`txrec.py` + the block envelope)

### 6.1 Envelope (`blocks` sub-db, key `hkey(height)`, msgpack value)

| Region | Value |
|---|---|
| Pre-fork | `{"h": block_hash_hex, "t": [[ts, addr, recip, amount, sig, pubkey_id, block_hash, fee, reward, op, openfield], ...]}` |
| Post-fork | `{"h": raw_block_hash, "a": [addrbytes blob, ...], "t": [txrec blob, ...]}` |

`"a"` is the per-block address dict: each distinct address/recipient string appears once, in first-seen
order. Block height is the key and is never stored in the value.

### 6.2 `txrec` layout (one element of post-fork `t[]`)

```
timestamp   varint centiseconds          (txfields)
addr_idx    varint                       -> envelope "a"
recip_idx   varint                       -> envelope "a"
amount      varint units                 (txfields)
signature   u8 tag || u16le len || raw   (sigbytes, self-delimiting)
pubkey_id   varint                       -> pk/pkr dedup
fee         varint units
reward      varint units
operation   u32le len || UTF-8           (free-form, uncapped post-fork)
openfield   u32le len || raw
```

`block_hash` is not stored per tx — it equals the envelope `"h"` and is filled on read. The sender address
string is still the input that selects the signature tag on write. The remaining u8 length prefixes (the
`"a"` blobs) rely on the consensus 56-char address truncation; `_lp1` raises if ever handed more than 255
bytes.

### 6.3 Pubkey dedup (`pk` / `pkr`)

Every stored pubkey — both regions — is replaced by a dedup id: `pk` maps `blake2b-32(key bytes) → u64 id`,
`pkr` maps `u64 id → key bytes` (the wire string's bytes). A 512-byte RSA key repeated across a sender's
history is stored once. Ids are assigned in commit order and are never reused.

### 6.4 Reconstruction (`BlockStore._expand`)

1. Decode `"h"` (raw → hex) and the `"a"` dict (`addrbytes.unpack_addr`).
2. For each `t[]` element: `bytes` → `txrec.unpack_row`, resolve the two address indices, fill `block_hash`
   from the envelope; a list → legacy row as stored.
3. Re-expand the pubkey id through `pkr`.
4. Prepend the height → the exact 12-field row as committed. `typed_row` restores the column types readers
   consume (height integer; timestamp/amount/fee/reward numeric; the rest text).

`rollback` runs the same reconstruction to know which index entries and balance deltas to reverse, so the
codec is exercised on every reorg path, not only on reads.

## 7. Pubkey-by-reference (consensus, `pkref`)

The RSA / ML-DSA / multisig public key is dropped from the **wire and the block-hash pre-image** for repeat
senders; single-sig secp256k1 / ED25519 carry none post-fork (the key is recovered from the signature or
derived from the address, and a non-empty key is rejected).

- **Registry:** the `pkref` sub-db, `address → u64 first-seen height || pubkey`, written by `commit_block`
  in the block's own transaction. **First appearance only**: an existing entry is never overwritten.
  Coinbase rows and empty keys never register.
- **Resolution:** `polysign.signerfactory.verify_tx_signature(..., registry=)` resolves an empty
  `public_key` through `block_store.PubkeyView` (`node.pk_registry`). Validation reads the committed tip,
  so only **prior** blocks can register a key — no intra-block ordering dependency. Referencing an
  unregistered address is rejected: the one new consensus rule.
- **Security anchor:** the resolved key passes through the same per-scheme verifier whose address-rebuild
  equality check binds key to address (the address is a one-way commitment to the key for every scheme),
  so a wrong key can never be registered or resolved. Multisig drops the whole redeem script.
- **Fork safety:** the block hash commits the **empty** wire field, so the hash is registry-independent.
  `rollback` deletes every entry whose first-seen height is above the new tip in the same transaction,
  which restores exactly the pre-reorg registry; `reindex` rebuilds it from the blocks.
- **Producers** default to inline keys; omission is opt-in (wallet / sync / pool), so the feature is dormant
  until a producer references.

## 8. Index sub-dbs

All maintained incrementally by `commit_block` (and reversed by `rollback`) inside the block's single
write transaction; `BlockStore.reindex` rebuilds them from `blocks` alone.

| Sub-db | Key | Value |
|---|---|---|
| `hashes` | raw block digest (28 B sha224 pre-fork, 32 B blake2b post-fork) | `hkey(height)` |
| `diff` | `hkey(height)` | difficulty text |
| `sig` | `signature[:56]` UTF-8 ‖ `0x00` ‖ s64 height ‖ u16 idx | empty |
| `addr` | role (`0` sender / `1` recipient) ‖ u8 len ‖ address ‖ s64 height ‖ u16 idx | empty |
| `bal` | address UTF-8 | 4 × i128 LE: credited amount, rewards, debited amount, fees (units) |
| `txid` | raw 32-byte content txid (post-fork rows) | `hkey(height)` |
| `pkref` | address UTF-8 | `hkey(first-seen height)` ‖ pubkey |
| `meta` | `b"schema"`, `b"txcount"`, `b"alloc"` | schema int text, `hkey(count)`, msgpack `{address: units}` |

Key-only sub-dbs carry their information in the key so a range scan over a prefix (`sig` by signature,
`addr` by role+address) yields positions in height order without touching block bodies.
Balance = credited + rewards − debited − fees; `meta[b"alloc"]` holds fixed allocations that no block row
carries (`ledger_genesis.MAINNET_ALLOCATIONS`) and `set_allocations` credits them into `bal` atomically.

## 9. Token / alias plugin store

`token_index.py` (owned by `plugins/tokens_aliases`, [doc/27](27-plugins.md)) is a separate LMDB env,
`tokenindex-<ledger>`, a derived projection that never feeds consensus. Its keys are raw byte layouts
(`token \0 party \0 H Q`, big-endian u64 heights and sequence numbers) so rollback is a height-range delete;
its values are frozen JSON / decimal strings / `b""`. Any move of those values to binary is a deliberate
format break with its own characterization re-baseline, never a silent sweep. Token amounts are unbounded:
a future binary form uses varint or u128, never u64.

## 10. Binary sync transport (`sync_codec.py`)

`GET /api/blocks/range?format=binary` carries the same logical payload as the JSON sync path in true bytes,
reusing the storage codecs. It is a negotiated transport swap, not a consensus change: no fork gate, JSON
fallback, and a binary-synced node derives byte-identical blocks to a JSON- or socket-synced one.

```
"BSY1" || varint nblocks
per block: varint height || u8 hashflag (0 raw digest / 1 verbatim) || u8 len || hash || varint ntx
per tx:    varint ts_cs || u8 len || addrbytes(addr) || u8 len || addrbytes(recip) || varint amount_units
           || sigbytes blob || u8 pkflag (0 base64-raw / 1 verbatim) || varint len || pubkey
           || u32le len || operation || u32le len || openfield
```

Amounts decode through `amounts.from_units` (the consensus form). Pre- and post-fork blocks both
round-trip.

## 11. Schema and upgrades

The ledger env carries `meta[b"schema"]` (`block_store.SCHEMA`). A fresh env is stamped on open. A node
that finds a populated ledger with a different schema **refuses to start** and names the fix:
`scripts/upgrade_ledger.py <ledger env>` with the node stopped. The upgrade replays the difficulty history
(`difficulty.retarget`, anchored on peers' published `/api/stats/difficulty`) into `diff`, then runs
`BlockStore.reindex` to build every index in §8. It is idempotent.

True-bytes bodies need no migration: they are written only for post-fork heights, and the reader dispatches
per element, so legacy records below the fork are read as they are.

## 12. Validation

- `tests/test_storage_codecs.py` — sigbytes / addrbytes / txfields / txrec / sync_codec round-trips,
  canonicality fallbacks, truncation and overlong-varint rejection.
- `tests/test_block_store.py`, `tests/test_ledger_store.py` — post-fork packing and byte-for-byte
  reconstruction, pre-fork records left legacy, straddling chains, atomic commit/rollback of every index,
  post-fork `txid` + `pkref` registration and rollback, allocations.
- `tests/test_replay.py` — `replay_verify` recomputes block hashes and txids from stored rows: 0 mismatches.
- `tests/test_multinode_integration.py` (`BISMUTH_RUN_MULTINODE=1`) — 3-node regnet mining across the fork:
  chain parity byte-identical across nodes, block bodies at pre- and post-fork heights identical across three
  independently built stores, post-fork txid heights identical, balances identical.
- `tests/test_two_node_signatures.py` (`BISMUTH_RUN_TWONODE=1`) — every signing scheme verifies and syncs
  across two nodes; `tests/test_two_node_api_sync.py` covers the binary transport.

## 13. Miner & pool impact

None beyond the fork-gated coinbase producer change. The bundled miner (`pool/optihash/optihash.py`) and
pool (`pool/optipoolware.py`) use only the JSON REST endpoints and the unchanged 8-field consensus wire
form; block_store, txrec and sync_codec are node-internal. The miner treats `blockhash` as an opaque string
in the Heavy3 input, so the 56→64-hex blake2b block hash flows through. Post-fork the pool, bundled miner
and regnet miner build the coinbase with the doc/41 mining header in the freed slots (signature slot = PoW
nonce, public_key slot = mining header + optional `"hf2"` signal); pre-fork they RSA-sign as before. The
coinbase is stored as an ordinary `txrec`: its slot strings go through the same codecs, and anything that is
not a canonical signature takes the opaque fallback, so it rebuilds exactly. The pool payout log uses the
node's returned canonical `txids[0]`.
