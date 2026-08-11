# 18 — The `hf2` hard fork (automatic, signal-activated)

> Companion to [`16-database-rework-plan.md`](16-database-rework-plan.md) and the roadmap
> [`17-roadmap.md`](17-roadmap.md). This collects everything that **changes consensus** into one
> deliberately-scheduled event, and separates it from the work that does not.

## The single most important distinction

Two kinds of modernization, and they must not be confused:

- **Mining-invariant (no fork, ship per-node, any time).** Storage (LMDB block store + pubkey dedup,
  balance index, reward *shadow*), transport/API, and **a GPU miner for *today's* Heavy3**. None of
  these change the block hash; old and new nodes produce and accept the identical chain. Proven by
  `test_replay` staying byte-identical with them active.
- **The `hf2` fork (changes the block hash / validation → coordinated).** Everything below. A node that
  doesn't upgrade forks off at the activation height. That is the point, and the reason it's gated.

## Activation — deterministic, automatic (✅ framework built)

`fork.py` carries the scheduler (`dynamic_fork_height`, `tests/test_dynamic_fork.py`):

1. Upgraded miners stamp `FORK2_SIGNAL` ("hf2") into their **coinbase openfield** (free-form block
   data — no rule change to *start* signalling). The fork-signal reader is per-era: it reads the
   openfield pre-fork (where lock-in signalling happens) and the coinbase **`public_key` slot**
   post-fork (→ doc/41).
2. Every node counts the signal from the **same chain**, so the activation height is computed
   identically everywhere — **deterministic, no off-chain peer survey, no split risk**.
3. When the trailing window (`FORK2_WINDOW`, 1000) is **all-signalled**, the fork **locks in**; it
   activates at the next round-1000 boundary past a burial margin (`FORK2_BURY`, ≥ rollback depth).
4. Your off-chain survey (the ~15-node network is small enough to eyeball) is the *confidence gate*;
   the actual decision is the chain's.

Framework — **built**: the coinbase-signal **writer** (`regnet.py`; upgraded miners set `hf2`), the
`/api/fork` readiness view (`rest_api.py` → `fork.fork_status`), and the live `block_height >= fork_height`
**gate** in `digest` — which already keys the VM, value custody, state-root enforcement, dynamic fees,
and the LWMA retarget. Each change below slots behind that one gate, replay-validated against pre-fork
blocks (unchanged).

## What `hf2` bundles

### A. Serialization & storage (the `# HARDFORK (doc/16)` sites) — design ready
Sign/hash **native integer units + a binary/struct tx encoding**, a bounded **content-hash txid**
(`blake2b(tx_content)`, replacing the `signature[:56]` slice), and **canonical sig/pubkey encoding**
(public key by reference — 1:1 with the address — and raw bytes, not base64). This is the bulk of a
block body. *Risk: low-moderate* — it's a representation change, fully replay-checkable: every pre-fork
block must still re-hash identically, every post-fork block round-trips through the new codec. The full
spec — native integer + binary tx encoding, pubkey-by-reference, raw-byte sig/pubkey, coinbase
compaction — folds into this SINGLE hf2 fork (no second fork) and lives in **doc/29**; Stage 0 has
shipped the dormant primitives `bismuth_serialize.signature_buffer_v2` / `tx_id_v2`
(`bismuth_serialize.py:76,92`).

#### A.1 Canonical txid + Ethereum-shape single-sig model — ✅ **implemented, fork-gated, tested**

This realizes section A's "content-hash txid" and "canonical sig/pubkey encoding" as an exact,
implemented spec (`tests/test_hf2_recoverable.py`, `tests/test_hf2_fork_transition.py`).
**Decision (final):** at `block_height >= node.fork_height`, ordinary
**single-sig** txs adopt the Ethereum-style identity model, borrowing only its *shape* — we keep
blake2b (the doc/18-D hash choice), **not** keccak/RLP/EIP-155.

**1. txid content (what is hashed).** The txid is computed over the **frozen
`bismuth_serialize.signature_buffer` fields** — exactly the six fields that have always been signed,
in order:

```
content_bytes = bismuth_serialize.signature_buffer(timestamp, address, recipient, amount, operation, openfield)
txid          = blake2b(content_bytes, digest_size=32).hexdigest()   # 64-hex, lowercase
```

`signature` and `public_key` are **excluded** from the content (just as Ethereum excludes
`v,r,s`). *Justification:* this is the smallest possible consensus delta — the six-field tuple is
already the frozen, characterization-locked signing pre-image (`signature_buffer`,
`bismuth_serialize.py:22`), so the txid is a pure function of bytes the network already agreed to
sign. Nothing new about *what* is committed; only that its blake2b-256 digest becomes the canonical
id and the signed message. Excluding the signature makes the id **content-derived and malleability-free**
(unlike `signature[:56]`, which is a slice of a non-deterministic ECDSA signature).

**2. The signed message becomes the txid.** Post-fork, single-sig signing/verification is over the
**32 raw txid bytes**, not the buffer:

```
message = bytes.fromhex(txid)          # 32 bytes
sig     = ecdsa_sign_recoverable(privkey, message)     # hasher=None — message is already the digest
```

So the relationship is `sig over unhex(blake2b(content))`. (Pre-fork stays `sig over content`
directly — the buffer itself.)

**3. Signatures become recoverable compact secp256k1.** Post-fork single-sig signatures are
**65-byte recoverable compact** `r(32) || s(32) || recovery_id(1)`, stored **hex** (130 chars), not
DER/base64. coincurve 21.x is present and provides this directly
(`PrivateKey.sign_recoverable(msg, hasher=None)` → 65 bytes;
`PublicKey.from_signature_and_message(sig, msg, hasher=None)` → recovered pubkey — verified working in
this environment). Enforce **low-s** (BIP-62) on accept so the id/sig pair is canonical.

**4. The `public_key` field is DROPPED for single-sig txs.** The signer is recovered, not carried:

```
recovered_pub = ecrecover(txid_bytes, sig)             # PublicKey.from_signature_and_message
if SignerECDSA.public_key_to_address(recovered_pub) != tx['address']:
    reject                                              # "spend from wrong address"
```

This is implemented in the single fork-aware verifier `SignerFactory.verify_tx_signature`
(`polysign/signerfactory.py:173-188`), called by BOTH the digester (`digest_tx.py:108`) and the
mempool (`mempool.py:402`): post-fork single-sig dispatches to ecrecover-over-txid, every other case
(pre-fork, post-fork RSA / ED25519 / multisig) keeps the legacy explicit-pubkey buffer check.
`public_key` is stored as the empty string post-fork (the
column stays for schema/replay continuity; pre-fork rows keep their base64 pubkey). *Justification:*
ecrecover makes the pubkey 1:1 derivable from (txid, sig), so carrying it is redundant — this is
exactly section A's "public key by reference". It only works for key-recoverable schemes, hence
single-sig (secp256k1 ECDSA) only.

**5. Scope — single-sig ONLY.** ecrecover/drop-pubkey applies **only** to ordinary single-sig
secp256k1 senders.
  - **RSA senders** are not key-recoverable and KEEP their legacy path post-fork: explicit pubkey +
    signing over the **frozen `signature_buffer`** (RSA can't ecrecover, and the recoverable-sig path is
    secp256k1-only). The coinbase is RSA-gated and is a special unsigned-value case — see note 8.
  - **MULTISIG senders** (`SignerFactory.address_is_multisig`, doc/23) KEEP their legacy scheme intact:
    explicit pubkeys + N-of-M verification over the **frozen `signature_buffer`**. Multisig does **NOT**
    sign the txid and does **NOT** drop pubkeys — only ordinary single-sig secp256k1 moves to the
    recoverable/ecrecover path. The digest multisig gate (`digest.py:572-585`) is unchanged.
  - **SHIELDED / RingCT** txs (doc/22) KEEP their ring-signature scheme and their own message binding
    (`shieldedv1._ring_message`), but a `shield:` tx is still an ordinary tx and still gets hf2's
    content-hash txid for indexing/lookup; ecrecover is never applied to them. Note shielded *consensus*
    validation is not part of hf2 — it is staged behind a separate `node.shielded_fork_height` (doc/22).

**6. txid computed ON READ; shape-dispatched lookup (every reader).** There is **NO `txid` DB column
and NO migration** — the canonical content-hash txid is computed **on read** from the stored row by
`essentials.format_raw_tx`, which rebuilds the signed `'%.8f'` amount string via `amounts.ledger_value`
(so it is **storage-mode agnostic** — same id whether the row holds a NUMERIC float or post-fork integer
units) and feeds the frozen six-field pre-image to `bismuth_serialize.tx_id` (= `blake2b`). Pre-fork rows
(or callers with no `fork_height`) keep the `signature[:56]` slice byte-for-byte.

Lookups dispatch on the **query string shape** so both eras resolve from one code path:

```
if len(q) == 64 and all(c in "0123456789abcdef" for c in q):   # post-fork content-hash txid
    # no signature prefix to match -> scan post-fork rows, recompute the content txid, compare
    for row in SELECT * FROM transactions WHERE block_height >= fork_height:
        if format_raw_tx(row, fork_height)['txid'] == q: return row
else:                                                          # legacy signature[:56] slice
    WHERE signature LIKE ?    (q + '%')                         # indexed prefix match (unchanged)
```

A 64-hex-lowercase string is unambiguously a post-fork txid (the legacy `signature[:56]` slice is
base64, so it contains `+/=` or uppercase and **cannot** match `^[0-9a-f]{64}$`); anything else is a
legacy prefix. Because the id is derived, not stored, there is nothing to migrate, no new column, no
index churn, and block hashes are untouched by construction (the txid never enters
`bismuth_serialize.TX_FIELDS` / the block-hash pre-image). See `rest_api._transaction`
(`rest_api.py:1163-1199`) for the live dispatch — the 64-hex branch is a BOUNDED, recent-first
streaming scan (`txid_scan_limit`, default 250000) with an optional `?from_height` to move the window
down, so an absent id can't drag the whole post-fork range (audit H-4).

**7. The integer-amount serialization freeze (byte-stability at the fork).** The txid content is only
byte-stable if `signature_buffer`'s `amount` (and `timestamp`) string form is frozen at the fork.
Today every signer/verifier reconstructs `amount` as `'%.8f' % float(amount)` and `timestamp` as
`'%.2f'` *before* calling `signature_buffer` (`wallet_helpers.py:29`, `digest_tx.py:49,56`,
`mempool.py` buffer build). doc/16 phase 2 (integer storage) is explicitly designed to keep these
*display/consensus-edge* strings identical — storage holds integer units, but the bytes fed to
`signature_buffer` are reconstructed via `amounts.from_units` to the **same `'%.8f'` decimal string**.
**Decision:** the hf2 txid content keeps the existing `signature_buffer` field bytes verbatim — i.e.
`amount` is the `'%.8f'` decimal string and `timestamp` the `'%.2f'` string, UTF-8 `repr`-of-tuple, as
frozen by `test_consensus_signature_buffer_is_frozen`. We do **not** switch the txid pre-image to raw
integer units, because (a) that would be a *second* byte change layered on the id change with no
benefit, and (b) keeping the frozen buffer means the same `signature_buffer` function serves both the
pre-fork direct-sign pre-image and the post-fork txid pre-image — one frozen byte form, characterization
test still valid. The fork's "native integer" win (section A) is realized in *storage/encoding*, while
the **consensus pre-image string form is the freeze point**: pin it at fork so the txid is deterministic
across every node regardless of `ledger_integer_amounts` mode. (If a future fork ever wants the integer
pre-image, that is a distinct, separately-characterized change — out of scope here.)

**8. Coinbase / reward tx.** The mining reward tx is special-cased: zero amount, RSA miner
address, never signature-verified (PoW + reward rules authorize it, not a sender signature). It still
gets a content-hash txid (it has the six fields), but it is not an ecrecover single-sig spend. Leave its
identity path as-is beyond computing the new txid for indexing. **Note (→ doc/41):** post-fork the
coinbase carries its mining header in the freed slots — the nonce in the `signature` slot and
the optional `hf2` signal in the `public_key` slot — and `operation`/`openfield` become optional,
uncapped, free-form miner data. (Pre-fork the nonce rode in the openfield, `miner.py:103-112`.)

**Map — every txid PRODUCER (`signature[:56]`) to change (file:line):**
- `essentials.format_raw_tx` `txid: raw[5][:56]` — `essentials.py:65-77` (the linchpin: feeds
  `blockstojson`/`blocktojsondiffs` and most REST/JSON tx shapes). Post-fork (`fork_height` passed and
  `raw[0] >= fork_height`): **compute** the content txid from the row via `bismuth_serialize.tx_id` over
  the `amounts.ledger_value`-normalised `'%.8f'` amount; pre-fork rows fall back to `raw[5][:56]`.
- `rpc_bitcoin.py` (`rpc_getblock` tx list, `rpc_getrawtransaction` txid, `getrawmempool`/`sendrawtransaction` ids) — **DONE**: emit the content txid via `essentials.format_raw_tx` / `bismuth_serialize.tx_id_v2_s` (a `_mempool_txid` helper for the no-block-height mempool/wire rows).
- `rpc_ethereum.py` (block `transactions`, tx `hash`, `txpool_*`/pending-filter/`eth_sendRawTransaction` ids) — **DONE**: same content-txid path.
- `tokensv2.py:88` (issue), `:153` (transfer; already has the `txid=="0" -> blake2bhash_generate`
  fallback) and `token_index.py` consumers (txid is just an opaque key there — feed the new id).
- `miner.py` / `regnet.py` build the wire tx; the **signer** (wallet send path,
  `send_nogui_noconf.py:124` `txid = signature_enc[:56]`) must switch to
  `blake2b(content).hexdigest()` and sign `unhex(txid)` post-fork.
- `ledger_explorer.py:175` (`x[5][:56]`), `web/explorer/index.html:135,163,320-322`
  (`.slice(0,56)` link + the `^[0-9a-fA-F]{56}$` search heuristic → add a 64-hex branch; display
  `t.txid` when present, fall back to `signature`).
- `digest`'s block writer / `to_db` path: **unchanged** — nothing is stored for the txid; it is derived
  on read, so the writer is storage-mode-only and the block-hash pre-image is untouched.

**Map — every txid LOOKUP (`signature LIKE`) to shape-dispatch (file:line):**
- `rest_api._transaction` — `rest_api.py:1163-1199` (64-hex → bounded recent-first scan of post-fork
  rows recomputing the content txid; else `WHERE signature LIKE ? LIMIT 1`); also `_submit_transaction`
  (`rest_api.py:420`) returns the computed post-fork txid.
- `apihandler_tx.api_gettransaction` (`apihandler_tx.py:31-38`), `api_gettransactionbysignature`
  (`:95-100` — this one is *by full signature*, keep as exact-signature match, but note post-fork sigs
  are hex compact), `api_gettransaction_for_recipients` (`:160-168`).
- `rpc_bitcoin` / `rpc_ethereum` tx-by-id — **DONE**: `block_store` bounded recent-first content-txid scan (SQLite `substr(signature,1,4)` TXID4 seek only as a pre-fork fallback when no block_store).
- `tokensv2.py:132` (the `openfield LIKE` token scan is unrelated — leave; only its `r[4][:56]` id
  derivation changes), `tokensv2.py:190` (`SELECT txid FROM tokens WHERE txid = ?` — the `tokens`
  side-index has its own `txid` column; feed it the computed content id, no `transactions` column
  involved).
- `check_tx.py:38,53` (`signature like ?`) — add the 64-hex branch (scan post-fork rows recomputing the
  content txid, as in `rest_api._transaction`).
- mempool ledger-dup check `mempool.py:410-416` (matches on full `signature`) — the dedup key stays the
  full `signature` (still present and unique on every tx, recoverable single-sig included); the
  `substr(signature,1,4)` index branch is unchanged.

**Map — the VM contract-address derivation (`vm_engine.contract_address`,
`vm_engine.py:29-35`) — ✅ IMPLEMENTED.** The deploy address now derives from the deploy tx's CONTENT
txid, **not** the (legacy, malleable) signature: `contract_address(seed)` returns
`blake2b(str(seed).encode(), digest_size=28).hexdigest()`, and the deploy path feeds it the content
txid via `_tx_id_of(row)` (`vm_engine.py:38-46`, `_deploy` at `:63-70`) — `blake2b` over the frozen
six-field pre-image, normalised through `amounts.ledger_value` so it is storage-mode-agnostic and
identical on the digest-execution and rebuild paths. Rationale: post-fork single-sig txs DROP the
signature for the recoverable compact form, and that sig is malleable in `recovery_id` / low-s in a way
the content-hash txid is not — so deriving the address from the canonical, content-derived txid makes
deploy addresses deterministic from tx content and independent of signature encoding. VM is
post-fork-only (inert pre-fork), so there was **no legacy contract address to preserve** — a clean
switch with no dual path. (`digest_size=28` keeps the contract address a 56-hex Bismuth-style address.)

**Gate.** Everything above keys on the single `block_height >= node.fork_height` (no second signal),
matching the existing VM / multisig / LWMA / blake2b gates in `digest.py`. Shielded value is **NOT**
in this list — it is staged behind a separate, default-`None` `node.shielded_fork_height` (doc/22), so
it is not part of hf2 (a plain config knob, not a version-bits signal — the "no second signal" claim for
hf2 still holds). Pre-fork:
`signature[:56]` id, DER/base64 sig, explicit pubkey, buffer-signing — **unchanged**, so no history
rewrite (doc/18 "Continuity"). Replay-validated: pre-fork blocks re-hash identically (the txid is never
stored and never enters the block-hash pre-image); post-fork blocks round-trip
(content→txid→sig→ecrecover→address). RSA, ED25519, native multisig, and shielded/RingCT keep their
existing legacy signing post-fork; ALL post-fork txs still carry the content-hash txid as their
canonical id.

**Non-consensus additions (this session).** Alongside A.1, several **read-only REST endpoints** were
added (no consensus impact, ship any time): `/api/proxy?target=` (same-origin relay to another node's
read-only `/api`), `/api/nodes` (peer browser, now de-duplicated), `/api/token/tx/{address}` (token
transfers for an address), `/api/alias/{name}` (resolve alias → owner), `/api/aliases/{address}`
(all aliases owned by an address), and the explorer dashboard set `/api/stats/{summary,monthly,
tx_per_month,new_addresses,rich_list,top_miners,largest_txs,market,difficulty,geo}` (`rest_stats.py`).
The `/api/proxy` relay was hardened (audit H-3): it pins the
validated public IP (no DNS-rebind TOCTOU), refuses redirects (no SSRF pivot), and enforces a
target-port allowlist + per-IP rate limit + global concurrency cap (`rest_api.py:76-83,540-601`). The
64-hex content-txid lookup is the bounded recent-first streaming scan noted above (audit H-4).

### B. Reward-sidechain cutover — foundation built (`reward_chain.py`)
Mint dev/hypernode rewards into the sidechain instead of negative-height mirror rows; balances read
ledger + sidechain. *Risk: moderate* — it's balance-preserving (proven for every address on regnet),
but it touches the consensus balance path, so it's replay- and invariant-gated.

### C. Difficulty stepping → **LWMA** — recommended design
Today's `difficulty.py` is a PID controller (60 s target) estimating hashrate, with an **asymmetric
per-block cap** (`MAX_DIFF_ADJUST = 1.0` up only) plus a separate emergency drop. It's convoluted and
swing-prone — and a small chain is exactly where that hurts (a pool hopping on/off swings difficulty
hard, stranding the chain at high difficulty when it leaves).

Replace it with **LWMA** (Zawy's Linear Weighted Moving Average), the de-facto standard retarget for
small PoW chains:
- Targets the same fixed block time; averages solvetimes over a short window (~60–90 blocks) weighting
  the most recent blocks highest → **fast, symmetric** response to hashrate changes.
- **Bounded** solvetime clamps resist timestamp manipulation; no oscillation, no hand-tuned PID gains.
- Adapted to emit Bismuth's bit-prefix difficulty domain.
- ✅ **Implemented** (`difficulty_lwma.py`, `tests/test_difficulty_lwma.py`): symmetric response (slow
  blocks lower difficulty by the same law fast blocks raise it — directly fixing the up-only ratchet),
  bounded per-retarget steps, single-timestamp-spike resistance, and convergence to the target block
  time under a feedback simulation — all unit-proven. Fork-gated, inert until activation.
*Risk: moderate* — consensus-critical but well-understood and widely battle-tested; deterministic and
unit-testable against recorded solvetime series before activation.

### E. Decentralized-apps VM — ❌ **REMOVED**
A post-fork RISC-V (RV32I) smart-contract layer was built and regnet-tested behind its own activation
gate, and then **deleted in full** — it never activated on any network and hf2 does not ship it. `vm:`
operations carry no consensus meaning: they are stored as ordinary inert transaction data.

### F. Dynamic fees → congestion-responsive base fee — ✅ **implemented, fork-gated, tested**
A smooth, clamped, *deterministic* base fee that tracks recent network **congestion** over a window
(`fee_dynamics.py`, the fee analogue of the LWMA); exposed at `/api/fee` for wallets. (The flat `shield:` surcharge was
**removed** when shielded value was decoupled from hf2 — `shield:` txs now pay the ordinary fee; it is
re-added later gated on `shielded_fork_height` if shielded is ever scheduled. Only the `vm:` surcharge
remains in hf2.) *Risk: low* — gated; pre-fork the static `BASE_FEE` is unchanged.

Congestion is measured by **block WEIGHT**, not just tx count: `weight = tx count + openfield bytes //
W_UNIT` (`block_store.BlockStore.recent_block_weights`, `W_UNIT=1000`), a gas/vbyte-style measure — so a
block of large RingCT/VM txs prices in its real footprint, not merely how many txs it holds (the baseline
is unchanged for all-tiny blocks, since each tiny tx is ~1 weight). `base_fee = static ×
clamp(avg(recent_weights)/TARGET_WEIGHT, 0.5×, 10×)` over `WINDOW=20` blocks. The weight window is read
from the **LMDB block store** (`digest.py:494-498` → `node.block_store.recent_block_weights`), **not**
SQLite — there is no SQLite on any post-fork path, and the earlier SQLite `recent_tx_counts` /
`recent_block_weights` helpers were removed (`essentials.py:291-293`). (This differs from the LWMA
retarget, which still reads solvetimes from SQLite — `difficulty.py` — at a different consensus boundary.)
The store is rolled back with the chain so the window is always canonical, making the fee consensus-
deterministic and storage-mode-independent (integer storage, doc/16, never touches `openfield`).
Manipulation-resistant: window-averaged (one block barely moves it), clamped (no runaway spike), and a
miner who stuffs blocks to inflate the fee pays the very fees they raise — the same bounded shape as
EIP-1559, but non-recursive so it needs no saved fee state across restarts. (`tests/test_fee_dynamics.py`,
`test_transactions.py`.)

**The window floors at block 2, never block 1.** Genesis is written when the ledger is CREATED, not through
`digest`, so it is the one height the forward-built block store can never contain. The consensus fee read is
deliberately **strict** (`recent_block_weights(..., strict=True)` raises on a missing window height, rather
than averaging a short window and deriving a divergent `base_fee`), so a window that reached height 1 raised,
the first post-fork block was rejected, and the node fell back forever:

```
block store missing height 1 in the dynamic-fee window [1, 9]
-- cannot compute a consensus-consistent base_fee
```

Regnet hit this on **every** run (fork at 10, `WINDOW` 20 → window `[1, 9]`), which is why the whole
post-fork regnet path was untestable: the chain stalled at height 9 and every test needing an included tx or
post-fork behaviour failed. A pristine-HEAD baseline had **116 failures vs 41** after the fix. The floor is
pure height arithmetic — identical on every node, no dependence on local store state — so the consensus fee
stays byte-identical network-wide, and on any chain more than `WINDOW` blocks past genesis (i.e. mainnet) it
never binds. If you ever see a pile of regnet failures around tx inclusion, **check the fork crossing first**.

### D. Heavy3 improvement — **optional, highest-risk; recommend caution**
Heavy3 (`sha224` → 1 GB memory-hard anneal → substring-prefix difficulty) is already GPU-mineable
(`gpuminer/` proves it), so **a GPU miner does NOT require changing Heavy3.** If we do change it:
- **Keep the memory-hardness** (the 1 GB junction file) — that's what limits per-GPU advantage and
  resists ASICs; dropping it on a low-hashrate chain invites a single farm to 51% it.
- Worth modernizing: the hash (`sha224` → `blake2b`), and the unusual *substring* difficulty metric
  (→ a clean threshold/leading-bits comparison that's easier to analyze).
- **This is the single most security-sensitive change.** Any PoW change has a transition window where
  hashrate is in flux; on ~15 nodes that window is dangerous.

✅ **The dual-algo MECHANISM is built and BUNDLED INTO hf2** (`mining_heavy3.diffme_heavy3(new_pow=…)`,
`miner.py`, `mining_heavy3.check_block`, gated in `digest.py`): the inner hash modernises
`sha224 → blake2b` (28-byte, same width); the 1 GB anneal and the
difficulty metric are unchanged. Miner and validator switch on `block_height >= node.fork_height` —
the SAME single activation height as A+B+C (the interim separate `pow2` fork was folded into hf2 on
2026-06-12). Tested on regnet (`test_miner.py::test_dual_algo_pow_switches`,
`tests/test_single_fork_validation.py` — the whole bundle incl. blake2b flips at one height live).
The consequence: stamping `hf2` asserts blake2b readiness too — the GPU kernels (`bis.cu` /
`bismuth.cl`) must swap the hash in lockstep at the hf2 height, so do NOT signal hf2 from a GPU setup
until its kernels are blake2b-ready. Full mining map: **doc/21**. (We kept the substring metric; only
the hash moved.)

## Continuity — what happens to the existing chain

The chain is **one continuous chain**; the fork is a boundary, not a restart. Nodes do **not**
reconstruct history.

- **Blocks below `fork_height` stay byte-for-byte, with their original hashes.** They were signed and
  hashed under the old rules, and each block commits to the previous block's hash — re-encoding even one
  would change every subsequent hash and snap the chain. History is immutable by construction.
- **At `fork_height` the new rules apply going forward only.** The first new-format block still
  references the last old-format block's hash, so the two halves join seamlessly. No new genesis, no
  re-sync, no balance reset — state carries straight across.
- **Validation is height-gated:** a node runs the old codec/rules for `height < fork_height` and the new
  ones at/above. Both rulesets live in every upgraded node. `replay_verify` re-hashes the whole chain
  exactly this way (old below, new above), which is how we prove no history is corrupted.
- **Storage is orthogonal to consensus.** A node *may* re-store the old blocks locally in the new
  scalable format (LMDB, pubkey-dedup, integer units) — that changes **no** block hash (it's behind the
  frozen boundary), so the storage wins apply to history too without touching the chain's identity. The
  consensus *serialization* of old blocks is never rewritten; only their local *representation* is.
- **Identifiers** follow the same rule: pre-fork txs keep their `signature[:56]` txids and base64
  sig/pubkey; post-fork txs use the content-hash txid and canonical encoding. Tools resolve both,
  height-gated.

So: nodes simply **continue with new-format data from the fork height**, on top of an unchanged past.

## Honest risk assessment & sequencing — DECIDED: one fork

**Decision (2026-06-12): A+B+C+D activate together at the single signalled `hf2` height.** The earlier
plan staged D (the PoW swap) behind its own later `pow2` fork to isolate the hashrate-transition risk;
that split was dropped in favour of one coordinated upgrade — one signal, one campaign, one boundary to
reason about on a small chain. The machinery is identical either way (`fork.dynamic_fork_height` is
signal-agnostic), so unifying cost nothing in code. What the decision trades:

- **Gained:** a single signalling campaign and flag height; no months-long interim where the network
  runs new rules on old PoW; the LWMA question of "which retarget absorbs the PoW transition" answers
  itself (LWMA and blake2b arrive at the same block).
- **Accepted:** the `hf2` signal now MEANS "ready for everything, including mining blake2b". Miners —
  the CPU path and especially the `gpuminer/` kernels (`bis.cu` / `bismuth.cl`) — must be blake2b-ready
  BEFORE stamping the signal, or their hashrate dies at the boundary. The window-of-flux risk that
  motivated the split is now managed by *when the network chooses to signal*, not by a second fork.

Still true: **now, with no fork**, finish the storage cutover (read path) and ship the GPU miner for
current Heavy3 (`gpuminer/`) — immediate value, zero consensus risk, grows the miner base before the
fork. The lock-in sidecar holds the single `hf2` key; stale `pow2` keys from older regnet runs are
ignored (wipe old regnet datadirs that carried split lockins before replaying them).

**Transition hardening (2026-06-12, proven by `tests/fork_transition_smoke.py`):**
- The digester derives/locks the fork height at the **top** of each block's processing, from confirmed
  history only — the rules a block is judged under derive from the chain *below* it. A node restarting
  on a chain already past activation with a **lost sidecar** therefore re-derives the height *before*
  judging (or mining against) anything, instead of wedging on era-mismatched PoW.
- The sidecar is loaded at startup in `setup_net_type()` — only there is `node.ledger_path` final, so
  a regnet/testnet node reads its **own** namespaced sidecar, not the mainnet-named one.
- `regnet.init()` deletes the regnet lock-in sidecar together with the chain it wipes (a stale lock-in
  against a fresh chain is the 2026-06-09 inconsistency class, intra-regnet); the test-only
  `BISMUTH_REGNET_KEEP=1` env escape keeps both across a restart for transition testing.

## The GPU miner is coupled to the PoW

`gpuminer/` implements *today's* Heavy3 exactly. When hf2 activates (it bundles **D**), `bis.cu` /
`bismuth.cl` must run the blake2b inner hash from that height — updated and re-validated on real
hardware BEFORE the network signals (this repo's CI has no GPU). The miner
and `mining_heavy3.py` must always compute the identical function — otherwise the miner emits invalid
blocks. See [`../gpuminer/README.md`](../gpuminer/README.md).
