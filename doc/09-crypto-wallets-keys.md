# 09 — Cryptography, wallets & keys

## Addresses & keys

A Bismuth (legacy) address is `sha224(public_key_pem.encode("utf-8")).hexdigest()` — a **56-char
hex** string. The default scheme is **RSA-4096**; public-key PEMs are 271 chars (1024-bit, legacy) or
799 chars (4096-bit) and that length is validated on load.

All signing, verification and address validation go through **`polysign`** (`SignerFactory`), which
offers a **menu of signature schemes**. As of the revival, **polysign is vendored in-tree** under
`polysign/` and its non-RSA signers are lazy-loaded, so an RSA-only mainnet node depends only on
`pycryptodomex` (see [14](14-known-issues-and-improvements.md)). `SignerFactory` selects the signer by
address shape: 56-hex → RSA; `Bis1…` → ECDSA/ED25519.

The available signers (`SignerType` → class, with the mainnet base58 address-version prefix where the
family is self-identifying):

| Signer | `SignerType` | Module | Mainnet address version | Notes |
|--------|--------------|--------|-------------------------|-------|
| RSA-4096       | `RSA = 1`        | `signer_rsa.py`       | `00` (56-hex)   | default mainnet scheme; only hard dep is `pycryptodomex` |
| ECDSA secp256k1| `ECDSA = 2`      | `signer_ecdsa.py`     | `4f545b` (`Bis1…`) | Bitcoin-style curve; pubkey embedded |
| ED25519        | `ED25519 = 3`    | `signer_ed25519.py`   | `03b86cf3` (`Bis1…`) | Edwards curve; pubkey embedded |
| ML-DSA-65      | `MLDSA = 4` (`MLDSA65`) | `signer_mldsa.py` | `064d4453` | post-quantum, NIST Cat 3 (FIPS 204 / Dilithium3); hash-of-pubkey address |
| ML-DSA-44      | `MLDSA44 = 5`    | `signer_mldsa.py`     | `064d4432` | post-quantum, NIST Cat 2 (Dilithium2); hash-of-pubkey address |
| ML-DSA-87      | `MLDSA87 = 6`    | `signer_mldsa.py`     | `064d4438` | post-quantum, NIST Cat 5 (Dilithium5); hash-of-pubkey address |
| secp256r1 (P-256) | `SECP256R1 = 7` | `signer_secp256r1.py` | `06523100` | classical NIST curve; passkeys / hardware secure-elements; hash-of-pubkey address |

The ML-DSA family and secp256r1 keep their wallet key as a **32-byte seed** (deterministic KeyGen /
key-derivation) and use a **hash-of-pubkey address** (`base58(version + sha256(pubkey) + checksum)`),
exactly like RSA — the full pubkey rides in the tx's `public_key` field. Their crypto deps
(`dilithium_py`, `cryptography`) are lazy-imported, so RSA-only nodes are unaffected. The post-quantum
ML-DSA signers are **inert on consensus** until a signalled `pq` fork (see [20](20-post-quantum.md)).

## `wallet.der`

Despite the `.der` extension, it is a **JSON text file** with three keys:

```json
{ "Private Key": "<PEM, or simplecrypt blob if encrypted>",
  "Public Key":  "<PEM>",
  "Address":     "<56-hex>" }
```

`essentials.keys_load[_new]` reads it (and auto-upgrades legacy `privkey.der`/`pubkey.der`);
`keys_check` generates a new RSA-4096 wallet on first run; `keys_save` writes it. There is no
explicit "encrypted" flag — an encrypted private key is detected by `RSA.importKey` failing, after
which `keys_unlock` prompts for a password and decrypts with `simplecrypt`. `wallet_keys.py` is a
minimal standalone reader/generator used by the `keygen` command.

## Signing & verification

`essentials.sign_rsa(timestamp, address, recipient, amount, operation, openfield, key,
public_key_b64encoded)` builds the canonical buffer
`str((str(timestamp), address, recipient, "%.8f"%amount, operation, openfield)).encode("utf-8")`,
signs it via `SignerFactory.from_private_key(...).sign_buffer_for_bis(buffer)`, verifies it
immediately, and returns the 8-field transaction tuple (or `False`). Verification anywhere uses
`SignerFactory.verify_bis_signature(signature, public_key_b64, buffer, address)`. The public key is
stored on-chain base64-encoded; **pre-fork** the txid is the first 56 chars of the signature.

**Post-fork (hf2) — content-hash txid.** Post-fork the canonical id is no longer a slice of the
signature but a **content-hash txid**: `blake2b-256` (`digest_size=32`, **64-hex**) over the same
frozen pre-image consensus signs — `(timestamp, address, recipient, amount, operation, openfield)`
(`bismuth_serialize.tx_id`). It is computed **on read** in `essentials.format_raw_tx` (gated on the
row's `block_height >= fork_height`), using the canonical `'%.8f'` amount string via
`amounts.ledger_value` so it is **storage-mode agnostic** (the post-fork block store keeps integer
units). There is **no `txid` DB column and no migration** — nothing is persisted. Lookup is
**shape-dispatched** (`rest_api._transaction`): a 64-char lowercase-hex query is treated as a
content-hash txid and resolved by scanning post-fork rows (`block_height >= fork_height`) and
re-deriving the hash; anything else falls through to the legacy signature-prefix `LIKE` match.
Pre-fork rows (and callers without a `fork_height`) keep the byte-identical `signature[:56]` slice.

**Post-fork single-sig secp256k1 — recoverable path.** An ordinary (non-multisig) secp256k1 sender
switches to an Ethereum-shape model: it signs the **32-byte content txid itself** via
`signer.sign_buffer_for_bis_recoverable(txid_bytes)`, producing a **65-byte recoverable** compact
signature (`r‖s‖recovery_id`) as lowercase hex; the **`public_key` field is dropped**. Verification
(`SignerFactory.verify_tx_signature` → `SignerECDSA.verify_bis_signature_recovered(sig, txid_hex,
address)`) recovers the signer via **ecrecover** and requires the derived address to equal the sender
— there is no explicit public key to check. **Low-s is enforced** (high-s/zero is rejected, never
normalised) so the signature is non-malleable. Everything else — all pre-fork txs, and post-fork
**RSA / ED25519 / native multisig / shielded (RingCT)** (shielded staged behind `shielded_fork_height`, doc/22) — keeps the legacy
`verify_bis_signature(signature, public_key, buffer, address)` path over the frozen
`signature_buffer` with an explicit public key (multisig signs the buffer with N-of-M explicit
pubkeys; it does **not** sign the txid). All post-fork txs still get the content-hash txid as their
canonical id regardless of which scheme verified them.

## Fees

`essentials.fee_calculate(openfield, operation='', block=0, base_fee=None, vm_surcharge=False)`
(`essentials.py:268`) = `base + len(openfield)/100000`, **+10** for `token:issue`, **+1** when
`openfield` starts with `alias=`; quantized to 8 dp (`block` is unused, kept for call-site
compatibility). `base` is the static `BASE_FEE = 0.01` (`essentials.py:40`) when `base_fee is None`.
This **static** formula is the **pre-fork** consensus fee — pinned by the characterization checks in
`tests/regnet_smoke.py` (and `tests/test_characterization.py`).

**Post-fork (hf2) — dynamic base fee + surcharges.** Post-fork the base is **demand-responsive**, not
the static `0.01`. Once per block (`digest.py:483-500`) `node.base_fee` is set to
`fee_dynamics.base_fee(BASE_FEE, recent_loads, target=TARGET_WEIGHT)` (`fee_dynamics.py:39`): a
window-averaged (`WINDOW=20`), clamped (`[MIN_MULT=0.5, MAX_MULT=10]×` the static fee) multiple of
`0.01` that scales with recent **block WEIGHT** — `tx count + openfield-bytes // W_UNIT` (a
gas/vbyte-style measure), **not** tx count alone. The weight window is read from the **LMDB block
store** (`block_store.recent_block_weights`, `block_store.py:148`), **never SQLite** — there is no
SQLite on any post-fork path. `digest` passes this `base_fee` (and `vm_surcharge=node.fee_post_fork`)
into every `fee_calculate` call (`digest.py:207-209, 248-250`). With `vm_surcharge` on, `vm:` txs add
`fee_dynamics.VM_SURCHARGE = 0.01` (gas). The `shield:` surcharge was **removed** when shielded was
decoupled from hf2 — `shield:` txs now pay the **ordinary fee** (the +1 EC-heavy surcharge returns
only if shielded is later scheduled, gated on `shielded_fork_height`) — `essentials.py:280-286`. Wallets read the live minimum from `/api/fee`
(`base_fee`, plus `static_base_fee`, `post_fork`, `vm_surcharge`, `target_weight`, `window`, the mult
clamps). The formula is a pure, deterministic function of the recent chain (no saved fee state across
restarts), and the store is rolled back with the chain so the window is always canonical.

## `simplecrypt.py`

A vendored copy of `simple-crypt`: AES-256-CTR with PBKDF2-HMAC-SHA256 key derivation (100,000
iterations in the latest format), HMAC-SHA256 integrity, 32-byte salt. Wire format
`[4-byte header][32-byte salt][ciphertext][32-byte HMAC]`. `encrypt(password, data)` /
`decrypt(password, data)` are the API; used only by `keys_unlock`.

## Aliases (`aliases.py` v1 / `aliasesv2.py` v2)

Human-readable names indexed into `index.db`'s `aliases(block_height, address, alias)` table,
first-come-first-served.

| | v1 (`aliases.py`, active) | v2 (`aliasesv2.py`) |
|---|---|---|
| trigger | `openfield LIKE 'alias=%'` | `operation = 'alias:register'` |
| alias value | openfield with `alias=` stripped | the openfield itself |
| coinbase filter | none | `reward = 0` |

The active version is selected by a comment toggle at the top of `node.py` (`import aliases` vs
`import aliasesv2 as aliases`). During the revival, `aliases.py` was changed to reuse the shared
lru-cached `essentials.replace_regex` instead of a private copy.

### Alias evolution ops (mutable ownership)

The `tokens_aliases` plugin (doc/27) adds three structured operations giving aliases **mutable
ownership** instead of one-shot first-claimant binding. These are **plugin-level (non-consensus)** —
projected into the plugin's own LMDB store from committed blocks; the base layer is unaffected:

| `operation` | `openfield` | Effect |
|---|---|---|
| `alias:register` | the alias to claim | claims `alias` for the sender (first claimant wins, like `alias=`) |
| `alias:transfer` | `recipient:alias` | moves ownership of `alias` to `recipient` — **owner-only** (sender must be the current owner) |
| `alias:free` | the alias to release | releases `alias` so anyone can claim it again — **owner-only** |

Coinbase rows are filtered (`reward == 0`), and structured ops are projected in one height-ordered
pass so a `transfer`/`free` always sees its prior `register`. The **legacy `alias=` first-claimant
convention still applies pre-fork** (and remains honored alongside the structured ops); the evolution
ops are the post-fork path to mutable, transferable names.

## Logging

`log.log(logFile, level="WARNING", terminal_output=False)` returns a `logging.Logger` (named
`'root'`) with a rotating file handler (5 MB × 2 backups) and a stdout handler; when
`terminal_output=False`, a filter shows only `Status:` lines and errors on the console. The node
stores it as `node.logger.app_log`.
