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
stored on-chain base64-encoded; the txid is the first 56 chars of the signature.

## Fees

`essentials.fee_calculate(openfield, operation='', block=0)` = `0.01 + len(openfield)/100000`, **+10**
for `token:issue`, **+1** when `openfield` starts with `alias=`; quantized to 8 dp (`block` is unused,
kept for call-site compatibility). This is the consensus fee — pinned by the characterization checks
in `tests/regnet_smoke.py`.

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

## Logging

`log.log(logFile, level="WARNING", terminal_output=False)` returns a `logging.Logger` (named
`'root'`) with a rotating file handler (5 MB × 2 backups) and a stdout handler; when
`terminal_output=False`, a filter shows only `Status:` lines and errors on the console. The node
stores it as `node.logger.app_log`.
