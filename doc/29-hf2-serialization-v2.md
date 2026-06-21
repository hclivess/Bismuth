# 29 — hf2 binary/integer serialization (authoritative spec)

> Generated 2026-06-21 by a multi-agent design pass cross-checked against the tree. The single hf2 fork
> includes the full serialization rework (user directive: one fork). Implementation staged below.

# Bismuth hf2 — Binary/Integer Serialization Rework: Authoritative Engineering Spec

Status: design-complete, pending implementation (doc/18 §A task #7). All citations are `file:line` under `/root/bismuth-claude/Bismuth/`. No code changed in producing this spec; load-bearing facts were re-verified against the tree.

---

## 1. Scope & Invariants

### 1.1 What this delivers
hf2 section A's "binary/struct tx encoding + native integer units" (`doc/18-hardfork-hf2.md:39-44`), which doc/18 marked "design ready" but never byte-specified. Five post-fork encodings, all gated on the single fork height:

1. **Block-hash binary pre-image** — blake2b-256 over a canonical binary per-tx encoding, replacing `sha224(str(8-tuples)+prev)`.
2. **Transaction pre-image (signing + txid)** — canonical binary, native integer units, replacing `str(6-tuple).encode()`.
3. **Pubkey-by-reference + raw-byte sig/pubkey** — drop or dedup the public key per signer scheme; store sigs/keys raw not base64.
4. **Coinbase compaction** — purpose-built ~50-byte record, drop the dead RSA sig+pubkey.
5. Fork-gating, characterization, staged rollout, and two latent-bug fixes that block a clean post-fork ledger.

### 1.2 Hard invariants (do not violate)
- **One fork signal.** Every gate reads `node.fork_height`. Never introduce a second signal. `amounts.LEDGER_INTEGER` (`amounts.py:20`) is a **storage** flag and stays decoupled from the **consensus height** gate (`digest.py` already separates them).
- **Gate by destination height, never a global mode.** The encoding is selected by the height of the block the bytes belong to (`block_instance.block_height_new`, confirmed at `digest_tx.py:148`), so any historical block re-serializes byte-identically regardless of node config or storage mode.
- **`None`-means-legacy.** `fork_height is None` ⇒ legacy path. Mainnet has `fork_height = None` until on-chain lock-in, so 100% of historical/current blocks take the frozen legacy branch by construction.
- **Pre-fork byte-identity.** The frozen functions `signature_buffer`, `tx_id`, `signed_message`, `block_hash` (`bismuth_serialize.py:22-54`) are NOT mutated; binary siblings are added beside them. Locked by `tests/test_characterization.py:177,185`.
- **One pre-image.** `tx_id` must keep delegating to the same buffer the signature signs (`bismuth_serialize.py:38`), so signing, txid, and verification can never drift to two byte forms.
- **Reconstruction discipline.** Store compact/binary; reconstruct the frozen legacy field strings on read for any pre-image that must stay frozen (the pattern of `amounts.consensus_amount`, `amounts.py:65-72`). The block hash (encoding #1) is the *one* pre-image that intentionally moves to integer/binary — that is the fork's purpose.
- **Replay-validated at every stage.** `replay_verify.py` must report 0 mismatches with `fork_height=None`, and 0 across a straddling chain with a low `fork_height`.

### 1.3 Open scope decision carried forward
doc/18 §A.7 (`doc/18-hardfork-hf2.md:142-158`) deliberately **froze the txid/signing pre-image on the legacy string buffer** for A.1, to avoid layering a byte change on the id change. This spec realizes the deferred integer pre-image. The block hash (encoding #1) is the unambiguous place to land integer/binary. Whether the **tx signing pre-image** (encoding #2-pre-image) also moves to binary for *all* post-fork schemes, or only single-sig secp256k1, is the one substantive open decision — see §5.D.

---

## 2. Concrete post-fork byte layouts

All multi-byte integers are **unsigned, little-endian** unless a field is explicitly noted big-endian. (The two source analyses differ on endianness for the block-hash codec; this spec **normalizes everything to little-endian** — see §5.A.) Canonical = exactly one valid encoding per logical tx; decoders MUST reject any non-canonical input.

### 2.0 Shared primitives
- `MAGIC = 0xB2`, `VERSION = 0x01`. `0xB2` cannot begin a legacy pre-image (legacy starts with ASCII `(` = `0x28`, `tests/test_characterization.py:182`), so legacy and v2 can never alias even un-gated.
- `timestamp_cs` = `int((Decimal(ts).quantize(Decimal("0.01"))) * 100)` — centiseconds. Quantize-to-2dp happens **before** scaling, matching `quantize_two` (`digest_tx.py:48`, `quantizer.py:25-31`). Preserves the legacy `'%.2f'` precision exactly with zero float drift. **u64** (years to ~5.8e9).
- `amount_units` = `amounts.to_units(amount)` (`amounts.py:23-25`), 1 BIS = 1e8. **u64 fixed width** (max 1.8e19 units ≫ supply). Fixed (not varint) because money math must have no canonicality footgun. Negative is unrepresentable; zero = `00*8` (legal: coinbase/zero-value carriers).
- Addresses encoded as the **canonical address-string bytes** (ASCII/UTF-8), length-prefixed — NOT decoded to raw key bytes. Rationale: the string is what every signer's `public_key_to_address` emits and what `address_validate` checks (`signerfactory.py:116-131`); raw form differs per scheme. Max length ≤ 60 (RSA 56 hex `signerfactory.py:49`; Bism multisig up to 60 `signerfactory.py:54`), so **u8** prefix suffices.
- `operation`: UTF-8 bytes, **u8** length prefix (cap `[:30]`, `digest_tx.py:59`).
- `openfield`: raw opaque bytes, **u32** length prefix (cap 100000, `digest_tx.py:60`; RingCT ~95KB `ringct.py:42` — u16 would overflow). Length is a **byte** count, not char count (fixes the latent char-vs-byte slice ambiguity in `digest_tx.py:60`; the fee/weight path already counts bytes, `fee_dynamics.py:31`).

### 2.A Transaction pre-image (signing + txid) — `signature_buffer_v2`

```
off   field            width   notes
0     MAGIC            u8      0xB2
1     VERSION          u8      0x01
2     timestamp_cs     u64 LE  centiseconds
10    amount_units     u64 LE  atomic units
18    addr_len         u8
19    address          var     addr_len bytes, ASCII/UTF-8
..    recip_len        u8
..    recipient        var
..    op_len           u8
..    operation        var     UTF-8
..    of_len           u32 LE  byte count
..    openfield        var     raw
```
`signature` and `public_key` are **excluded** (unchanged from A.1, `doc/18-hardfork-hf2.md:63`). Fork height is NOT in the pre-image (contextual).

Chain (unchanged shape from A.1): `txid = blake2b(signature_buffer_v2(...), digest_size=32).hexdigest()` (64 hex); single-sig secp256k1 signs `signed_message(txid)` = the 32 raw bytes, recoverable compact 65-byte r‖s‖v, low-s enforced (`signer_ecdsa.py:146-172`).

**Worked example** — `ts=1500000000.00`, `amount=1.00000000`, sender ECDSA `Bis1VbuFefph6dRwXNTCN2paQbpfkPmAm9` (34 chars), recipient RSA 56-hex, `op="vm:call"`, `openfield="x"`:
```
B2 01                                            MAGIC, VERSION
00 1C F2 EF 22 00 00 00                          ts_cs = 150000000000
00 E1 F5 05 00 00 00 00                          amount = 100000000 units
22  <34 ASCII bytes of Bis1Vbu...>               addr
38  <56 ASCII bytes of the hex addr>             recip
07  76 6D 3A 63 61 6C 6C                         op = "vm:call"
01 00 00 00  78                                  openfield = "x"
```
Total = 2+8+8+(1+34)+(1+56)+(1+7)+(4+1) = **123 bytes**. `txid = blake2b(those 123 bytes, 32).hex()`.

**Decoder rejection (consensus):** MAGIC≠0xB2 or VERSION≠0x01; truncation; **trailing bytes** after openfield; `of_len > 100000`; any prefix overrunning the buffer; address/recipient failing `address_validate` (`digest_tx.py:97-100`); amount decodes only as u64 (no negative bit pattern).

### 2.B Block-hash binary encoding — `block_hash_v2`

**Algorithm: blake2b-256, drop sha224.** hf2 already commits to blake2b for txid (`bismuth_serialize.py:38`) and PoW (`mining_heavy3.py:87-88`); sha224 is the only orphan left in the consensus hot path. Output is **64-hex** (full 256-bit; the block hash is an opaque chain link, not an address, so no 56-hex width-compat constraint).

Per-tx bytes fed into the hash (in `TX_FIELDS` order, post-fork value forms). Note this pre-image **includes sig+pubkey** (unlike the signing pre-image — the block hash commits to the canonical sig):
```
tx_bytes =
  u64 LE  timestamp_cs                       integer centiseconds (NOT '%.2f')
  u8  addr_len  || address                   ASCII, <=56
  u8  recip_len || recipient                 <=56
  u64 LE  amount_units                       integer atomic units (NOT '%.8f')
  u16 LE  sig_len   || sig_bytes             RAW signature bytes (see 2.C), not base64
  u8      pubref                             0 = inline/recovered/none, 1 = by-address (see 2.C)
  u16 LE  pubkey_len || pubkey_bytes         RAW; pubkey_len=0 when pubref!=0 or key dropped
  u8  op_len    || operation                 <=30
  u32 LE  of_len    || openfield             <=100000
```
Block pre-image:
```
block_preimage = u32_LE(tx_count) || concat(tx_bytes...) || prev_hash_bytes
block_hash_v2  = blake2b(block_preimage, digest_size=32).hexdigest()
```
`prev_hash_bytes` = `bytes.fromhex(prev)` for a 64-hex post-fork parent; for the **first** post-fork block (parent is the last pre-fork 56-hex sha224 hash) use the parent string UTF-8-encoded — a one-time documented boundary special case.

Determinism is *stronger* than legacy `str(tuple)` (which depended on Python `repr` quoting/escaping); explicit lengths make two implementations unable to diverge on, e.g., an openfield containing a quote.

The fork-aware call site (`digest.py:521`) becomes:
```python
block_instance.block_hash = bismuth_serialize.block_hash_at(
    block_instance.block_height_new, getattr(node, "fork_height", None),
    block_instance.transaction_list_converted, node.last_block_hash)
```

### 2.C Pubkey-by-reference + raw-byte sig/pubkey

Per-scheme matrix (drives whether the pubkey can be omitted):

| Scheme | Address is | Pubkey recoverable? | Post-fork handling | Source |
|---|---|---|---|---|
| secp256k1 single-sig | hash (ripemd160∘sha256) | Yes, ecrecover | **DROPPED** (already done, A.1) — `pubref=0,pubkey_len=0` | `signer_ecdsa.py:79-99,146-172` |
| ED25519 | **embeds raw 32B pubkey** | Yes, from address | **DROPPED** (new) — strip 1B version+4B checksum → 32B key | `signer_ed25519.py:90-106` |
| RSA | `sha224(pem)` | No | **by-reference** (store-once), inline on first use | `signer_rsa.py:62` |
| ML-DSA 44/65/87 | `sha256(pub)` | No | **by-reference**, inline on first use | `signer_mldsa.py:84-96` |
| native multisig | redeem-script hash | No (N keys) | explicit keys over frozen buffer; each constituent key may be by-reference | doc/23, `doc/18:109-112` |

**Store-once-by-address.** The address *is* a commitment to the key (RSA `sha224(pem)`, ML-DSA `sha256(pub)`), so it is the dedup key. Promote the existing storage-local `pk`/`pkr` dedup (`block_store.py:67-94`, content-hashed by `blake2b(pubkey,32)` because 1068B exceeds LMDB's 511B key cap) to a consensus-visible registry keyed by **sender address**:
- First tx ever from address `A`: carries full raw pubkey inline (`pubref=0, pubkey_len>0`); validator checks the address-binds-to-pubkey relation (RSA `sha224(pem)==A`, ML-DSA `sha256(pub)==A`) and records `A→pubkey`.
- Every subsequent tx from `A`: `pubref=1, pubkey_len=0`. Validator fetches the stored key by the tx's `address` field (already in every tx) and verifies over the frozen buffer.
- **Reject** a `pubref=1` tx whose address has no prior on-chain pubkey (can't verify; also blocks referencing an unregistered key).

`pubref` collapses to one boolean in the wire/body form (the address is the lookup key); the integer-id `pkr` table remains the *physical* storage representation only.

**Raw-byte sig/pubkey.** Today wire forms are base64 (RSA double-b64 `signer_rsa.py:118`; others single). The `verify_bis_signature_raw` paths already exist for every signer (`signer.py:128`, `signer_rsa.py:142,150`, `signer_ecdsa.py:120`, `signer_mldsa.py:120`) and re-wrap as needed (RSA rebuilds PEM via `normalize_key(b64encode(...))`). Post-fork store raw bytes and dispatch to `verify_bis_signature_raw` (`signerfactory.py:160-163`). ~25% saving on every sig/pubkey.

Approx per-tx savings: secp256k1 ~1.7KB → ~65B; ED25519 pubkey→0, sig 88→64B; RSA-after-first pubkey 1068→0, sig 684b64→256B raw; ML-DSA-65-after-first pubkey ~2.6KB→0.

### 2.D Coinbase compaction

The coinbase carries no value and is authorized by **PoW + reward rules, never a spend signature** (`doc/18:160-164`); its RSA sig+pubkey are dead weight. Compact record (last tx of the block):
```
u8      version            0x01 = hf2 compact coinbase
u32 LE  timestamp_cs       round(block_ts*100); == block timestamp gate (digest.py:506)
u8      addr_len || miner_address   RSA 56-hex (coinbase is RSA-gated, digest_tx.py:66)
u8      flags              bit0: hf2 readiness signal; bit1: VM state-root present
[if bit1] vm_state_root    32 raw bytes (blake2b-256 pre-state root; NOT the "vmsr"+64hex string)
u64 LE  nonce              PoW nonce as integer (replaces '%0x' hex string + concat)
```
- `amount` (0), `operation` ("0"), `recipient` (==miner) are omitted constants (`digest_tx.py:64-67`).
- Fork signal: 1 flag bit instead of the 3-char "hf2" string (kept for `fork.has_fork_signal`/window counting, `fork.py:29-31`).
- VM state-root: 32 raw bytes instead of `"vmsr"+64hex` (96 chars → 32 bytes, replacing `vm_engine.py:150-168`).
- nonce: u64 (`getrandbits(64)` is already 64-bit, `miner.py:86`).

Total ~50 bytes vs ~1.9KB today. Validator: (1) reconstruct PoW `openfield`/`nonce` strings deterministically and run `mining_heavy3.check_block(..., new_pow=True)` (`mining_heavy3.py:101-122`, `digest.py:533`) so PoW stays bit-identical to what the miner solved; (2) read `vm_state_root` directly and enforce against `node.vm_state_root` (`digest.py:550-570`; `flags bit1` required once VM is active — the mandatory-root rule `digest.py:561-566`); (3) validate reward by reward rules (no sig); (4) still derive a content-hash **txid** over the six logical fields (via `signature_buffer`/`from_units` reconstruction) so explorers/`format_raw_tx` resolve it. The miner address is committed *into* the PoW pre-image (`address+openfield+blockhash`, `miner.py:87`), so the payout address can't be swapped without redoing PoW.

---

## 3. Blast-radius checklist (every site to change, grouped)

Source of truth: `bismuth_serialize.py:22-54`. Frozen by `tests/test_characterization.py:177-192`, `tests/test_hf2_recoverable.py:31-38`, `tests/test_amount_migration.py:36`, `tests/test_headers_sync.py:28`.

### 3.1 Codec — `bismuth_serialize.py` (add beside frozen, never mutate)
- Add `signature_buffer_v2(ts_cs:int, addr, recip, amount_units:int, op, openfield) -> bytes` (§2.A).
- Add `block_hash_v2(tx_list, prev) -> str` (§2.B), `tx_id_v2`, coinbase codec.
- Add dispatchers: `signature_buffer_at(height, fork_height, ...)`, `block_hash_at(height, fork_height, ...)`. Make `tx_id` delegate to `signature_buffer_at` so txid auto-tracks the signing buffer (one-pre-image invariant).

### 3.2 Consensus gate sites (route through dispatchers)
- `digest.py:521-523` — block hash → `block_hash_at(block_height_new, fork_height, ...)`. **The single change that makes the block hash fork-aware.** (`block_height_new` is the destination height, `digest_tx.py:148`.)
- `digest_tx.py:120-131` (`to_tuple`) — add `to_tuple_v2` (integer units + raw fields), or have `block_hash_v2` pack raw fields itself so `to_tuple` stays frozen for legacy.
- `signerfactory.py:187` — non-ecdsa branch must pick `signature_buffer` vs `_v2` by `post_fork` (today hard-codes legacy). The ecdsa branch (`:184`) already uses `tx_id` and is fine once `tx_id` is dispatcher-backed.
- `digest_tx.py:106-118` (`Transaction.validate`) and `mempool.py:392-408` (`merge`) — already compute `post_fork` from destination height and route through `verify_tx_signature`; no structural change.
- `essentials.py:72-77` (`format_raw_tx`) — already gated on `fork_height`; ensure post-fork `tx_id` pre-image == `signature_buffer_v2`.
- `vm_engine.py:45` (`_tx_id_of`) — contract-address seed picks up v2 via `tx_id`.
- `replay_verify.py:66` — thread optional `fork_height` param into `verify_blocks`; default `None` ⇒ existing legacy behavior; call `block_hash_at(height, fork_height, ...)`.

### 3.3 Highest-risk parallel copies (NOT routed through the module — fix or they silently diverge)
- `node.py:1248-1252` (`verify`, startup ledger-sig check) — inlines `str((...)).encode()` and verifies with NO fork awareness. **Latent bug #1, §6.** Route through `verify_tx_signature` with `post_fork = fork_height is not None and int(row[0]) >= fork_height`.
- `node.py:958-962` (deprecated `txsend`) — inline `str(remote_tx)` buffer. Deprecated; gate or leave pre-fork-only.
- `send_nogui_noconf.py:118-124` — inline `str(transaction)` buffer + `txid = signature_enc[:56]`. **Latent bug #2, §6.**
- `genesis.py:74-80` — own sign+hash; chain-creation only, pre-fork, leave.

### 3.4 Legacy `txid == signature[:56]` consumers (mis-resolve once txid is content hash)
`apihandler_tx.py:33,37,99`; `apihandler.py` legacy `api_gettransaction`; `rest_api.py:1177`; `rpc_bitcoin.py:124`; `rpc_ethereum.py:112`; `check_tx.py:38,53`; `ledger_explorer.py:175`; `plugins/tokens_aliases/__init__.py:145` (has blake2b fallback at `:147`). These already have a dual-mode pattern in `rest_api.py:1141-1181` (64-hex → re-hash scan; else `signature LIKE`); extend the same dual-mode lookup.

### 3.5 Replay/dedup index keyed on signature (not txid)
`mempool_sql.py:41,45`; `db_migrations.py:25` (`TXID4_Index` on `substr(signature,1,4)`); `digest.py:98-116` (`_signature_exists_in_ledger`); `mempool.py:425`. Replay protection keys on the **signature**, not the txid — unaffected by txid semantics, but verify the post-fork 65-byte recoverable sig is still a stable dedup key.

### 3.6 Signers / wallets / CLI (sign or compute txid)
SIGN: `signer_rsa.py:162-171`, `signer_ecdsa.py:131-148`, `signer_ed25519.py:136-142`, `signer_mldsa.py:123-127`, `signer_multisig.py` + `multisig_wallet.py:79-106`, `signer_secp256r1.py`, `signer_btc.py`, `signer_crw.py`. VERIFY: `signerfactory.py:151-188`, per-signer `verify_bis_signature(_raw)`. Wallets: `wallet_helpers.py:24-39` (`sign_rsa`), `hd_wallet.py:221-246`, `multisig_wallet.py:94-106`. Demos/tests: `tests/_lite_client.py:92-142`, `_lmdb_demo.py:40`, `web/predictionmarket/relay.py:48-55`.

### 3.7 Storage boundary (references frozen form, does not redefine — keep both branches)
`amounts.py`, `db_migrations.py`, `migrate_amounts.py`, `digest.py:184-229` (12-field `block_transactions` row; integer units when `LEDGER_INTEGER`, but block hash uses the separate `to_tuple`, so storage mode does not affect consensus).

### 3.8 Non-impacts (verified)
- Shielded/RingCT carrier sigs sign their own domain-separated message (`shieldedv1.py:277-280`, `ringct.py` MLSAG); payload rides in `operation`+`openfield` and is transitively committed by the outer sig. `shieldedv1.validate_block` (`:704-759`) reads 12-field rows by index — depends on row shape, not buffer encoding.
- PoW (`mining_heavy3.py`, `miner.py`, gpuminer) consumes `block_hash` output but is separate from tx serialization (already blake2b post-fork).
- Field-truncation ceilings (consensus-frozen): sig`[:684]`, pubkey`[:1068]`, op`[:30]`, openfield`[:100000]` at `digest_tx.py:57-60`, `mempool.py:350-368`; multisig depends on `SIG_FIELD_MAX=684`/`PUBKEY_FIELD_MAX=1068` (`signer_multisig.py:23-27`). The v2 encoding replaces truncation-slicing with exact-length encode; preserve the ceilings as decoder bounds.

---

## 4. Staged implementation plan

Order: risky consensus byte-change lands **last**, behind the height gate; each stage independently regnet-green + replay-validated. Mainnet stays inert throughout (`fork_height = None` until lock-in).

### Stage 0 — Codec + dispatchers, dormant
- **Files:** `bismuth_serialize.py` (add `signature_buffer_v2`, `block_hash_v2`, `tx_id_v2`, coinbase codec, `signature_buffer_at`, `block_hash_at`); `tests/test_characterization.py` (add v2 + dispatch vectors, §4.tests).
- **Gate:** none wired into consensus.
- **Test:** suite proves codec determinism and dispatcher returns legacy for `None`/pre-fork.
- **Rollback risk:** none — no consensus site calls v2.

### Stage 1 — Gate the block-hash call site
- **Files:** `digest.py:521`.
- **Gate:** `block_hash_at(block_height_new, fork_height, ...)`; with `fork_height=None` this is a pure no-op (legacy).
- **Test:** full regnet mine + `tests/test_replay.py` green (no fork ⇒ legacy ⇒ identical hashes).
- **Rollback risk:** must pass the **destination** height (`block_height_new`), not `last_block`. Verified present (`digest_tx.py:148`).

### Stage 2 — Gate the signature buffer + txid
- **Files:** `signerfactory.py:187` (non-ecdsa branch picks `_v2` by `post_fork`); make `tx_id` delegate to `signature_buffer_at`; `digest_tx.py:120` (v2 tuple/raw fields).
- **Gate:** `post_fork` from destination height (already so: `mempool.py:398` uses `tip+1`, `digest_tx.py:107` uses `block_height`).
- **Test:** regnet signs/verifies pre-fork txs unchanged; a forced-low-`fork_height` regnet signs/verifies v2 txs.
- **Rollback risk:** mempool and digester must compute `post_fork` from the same destination height — they already do.

### Stage 3 — Pubkey-by-reference, raw sig/pubkey, coinbase compaction
- **Files:** `block_store.py:67-94` (promote pk/pkr to address-keyed registry), `signerfactory.py:160-163` (raw verify dispatch), `signer_ed25519.py` (recover key from address), coinbase build/validate in `miner.py`, `digest.py:533,550-570`, `vm_engine.py:150-168`.
- **Gate:** all on `block_height_new >= fork_height`.
- **Test:** drive a regnet chain past a low `fork_height` with `tests/fork_transition_smoke.py` / `tests/test_hf2_fork_transition.py` so blocks straddle the boundary.
- **Rollback risk:** the **straddle block** (first v2 block, `previous_hash` = last legacy 56-hex hash). `block_hash_v2` takes `previous_hash` as a hex string and special-cases the boundary; verify it links. Reject `pubref=1` from an unregistered address.

### Stage 4 — Wallet/CLI/native-integer cleanup (post-fork only)
- **Files:** `node.py:1248-1252` (bug #1), `send_nogui_noconf.py:118-124` (bug #2); delete `'%.8f'`→units→`'%.8f'` double-conversions (`digest.py:224`, `node.py:1243`) **only on the post-fork path**; `hd_wallet.py`, `tests/_lite_client.py`.
- **Gate:** post-fork branch against the target node's fork status (`/api/fork`, `rest_api.py:655`).
- **Test:** §6 fixes verified; pre-fork paths byte-identical.
- **Rollback risk:** **do not delete the legacy reconstruction** — every pre-fork row still needs it. Keep both branches.

### Characterization tests (Stage 0, never edit the two legacy asserts)
Keep `tests/test_characterization.py:177` (`signature_buffer`) and `:185` (`block_hash`) asserting legacy vectors verbatim. Add beside them:
- `test_consensus_signature_buffer_v2_is_frozen` — hex-pin the §2.A Example-1 123-byte form.
- `test_consensus_block_hash_v2_is_frozen` — hex-pin a v2 block hash.
- `test_serialize_dispatch_selects_by_height` (load-bearing): asserts `block_hash_at(fork-1, fh)==legacy`, `block_hash_at(fork, fh)==v2`, `block_hash_at(fork, None)==legacy`. This locks the `>=` boundary and the `None`-means-legacy invariant — the guarantee of pre-fork byte-identity.

### Replay/parity gate per stage (zero mismatches)
- **Pre-fork invariance (every stage):** `replay_verify.py` against a stored ledger with `fork_height=None` reports `0 mismatch(es)` (as-stored and integer-roundtrip); `tests/test_replay.py` enforces in CI.
- **Boundary parity (stages 1-3):** replay a *straddling* regnet chain — blocks `< fork_height` via legacy, `>= fork_height` via v2, whole chain reproduces stored hashes, straddle block links.
- **Cross-transport parity (stage 3):** socket sync (`node.py` verify) and REST sync (`rest_api.py:439`, `api_sync.py`) must derive byte-identical post-fork blocks at every height across the fork (`tests/test_api_sync.py`, `tests/test_headers_sync.py`). Same concern `amounts.consensus_amount` documents (`amounts.py:65-72`).
- **Consensus-equivalence:** run the suite under `tests/config_custom.txt` (regnet, integer storage on) and confirm the integer-storage path and v2 path agree on hashes through the boundary.

---

## 5. Open decisions (with recommended default)

**A. Endianness of the binary codec.** The tx-encoding analysis specced little-endian; the block-hash analysis specced big-endian. *Recommendation: little-endian everywhere.* Matches `u64::to_le_bytes()`/`u32::to_le_bytes()` for the "byte-for-byte reproducible in a future Rust client" goal; pick once and pin in the characterization vectors.

**B. Does the binary tx pre-image apply to ALL post-fork schemes, or only single-sig secp256k1?** A.1 explicitly froze the **string** buffer for RSA/ED25519/native-multisig/shielded post-fork (`doc/18-hardfork-hf2.md:106-112`), so naively this leaves *two* live pre-image forms after hf2 (v2 binary for single-sig ecdsa; legacy string for the rest). *Recommendation: ship v2 binary for single-sig secp256k1 only* (the dominant path, already ecrecover) and keep the legacy buffer for the other schemes, matching A.1's freeze; revisit unifying in a later fork. This is the one decision that changes whether `signature_buffer_at` returns one or two post-fork forms — confirm with the user before Stage 2, since it is a consensus scope question, not an implementation detail.

**C. Block-hash width.** 56-hex (sha224, blake2b digest_size=28) vs 64-hex (blake2b-256). *Recommendation: 64-hex.* The block hash is an opaque chain link, not an address; no width-compat reason to truncate. Pin the boundary special-case (first post-fork block's `previous_hash` is 56-hex).

**D. Varint vs fixed-width for lengths/amount/timestamp.** *Recommendation: fixed-width* (as specced). Eliminates the LEB128 non-minimal-encoding canonicality footgun on money/length fields; the ≤~10B/tx overhead is negligible against an openfield-dominated body; trivially portable (no minimal-form rule to mis-port).

**E. Fork-signal/lock-in machinery (`fork.py`).** *Recommendation: no change.* It is already single-signal and correct.

---

## 6. Two latent bugs to fold in (Stage 4, post-fork-only branches)

**Bug 1 — `node.py:1248-1252` (`node.verify`, startup ledger-sig check).** Hard-builds the **legacy** buffer `str((db_timestamp, db_address, ...)).encode("utf-8")` (line 1248) and verifies via `SignerFactory.verify_bis_signature` (line 1252) with **no fork awareness**. Post-fork rows are v2-signed (single-sig ecdsa over the content txid, no explicit pubkey), so this loop flags every post-fork tx as "Signature validation problem" and inflates `invalid`. **Fix:** route through the single authority `SignerFactory.verify_tx_signature(post_fork, db_timestamp, db_address, db_recipient, db_amount, db_operation, db_openfield, db_signature_enc, db_public_key_b64encoded)` with `post_fork = node.fork_height is not None and int(row[0]) >= node.fork_height`. (Line 1243 already handles integer-units amount via `from_units`; the bug is purely the missing fork branch.)

**Bug 2 — `send_nogui_noconf.py:124` (`txid = signature_enc[:56]`).** Computes txid as the legacy 56-char signature slice and signs RSA over the legacy buffer (lines 118-122). Against a post-fork node this yields (a) a wrong txid that won't match the node's content-hash txid, and (b) for an ecdsa wallet, an unverifiable signature (post-fork the node expects ecrecover over `tx_id`). **Fix:** query the node's fork status (`/api/fork`, `rest_api.py:655`) or next-block height; if destination `>= fork_height`, build the pre-image via `signature_buffer_v2`, compute `txid = tx_id(...)`, and for ecdsa wallets sign `signed_message(txid)`. Minimal correctness even before full v2 signing: replace line 124 with `bismuth_serialize.tx_id(...)` when post-fork so the printed txid matches what the node indexes (`essentials.format_raw_tx:74`).

Both fixes are post-fork-only branches; pre-fork paths stay byte-for-byte unchanged, preserving `replay_verify`/`test_replay` and the legacy characterization vectors.

---

## 7. Files to touch (summary)
`bismuth_serialize.py` (codec + dispatchers) · `tests/test_characterization.py` (v2 + dispatch vectors) · `digest.py:521` (gate block hash), `:224,550-570` · `digest_tx.py:120` (v2 tuple) · `signerfactory.py:187,160-163` (gate non-ecdsa buffer, raw verify) · `signer_ed25519.py` (recover key from address) · `block_store.py:67-94` (address-keyed pubkey registry) · `miner.py`, `vm_engine.py:150-168` (coinbase compaction) · `replay_verify.py:66` (optional `fork_height`) · `node.py:1248-1252` (bug 1) · `send_nogui_noconf.py:124` (bug 2) · dual-mode lookup extensions in §3.4. No changes to `fork.py`.
