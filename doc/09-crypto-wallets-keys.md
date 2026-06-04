# 09 — Cryptography, wallets & keys

## Addresses & keys

A Bismuth (legacy) address is `sha224(public_key_pem.encode("utf-8")).hexdigest()` — a **56-char
hex** string. The default scheme is **RSA-4096**; public-key PEMs are 271 chars (1024-bit, legacy) or
799 chars (4096-bit) and that length is validated on load.

All signing, verification and address validation go through **`polysign`** (`SignerFactory`), which
also supports ECDSA / ED25519 / BTC / CRW addresses. As of the revival, **polysign is vendored
in-tree** under `polysign/` and its non-RSA signers are lazy-loaded, so an RSA-only mainnet node
depends only on `pycryptodomex` (see [14](14-known-issues-and-improvements.md)). `SignerFactory`
selects the signer by address shape: 56-hex → RSA; `Bis1…` → ECDSA/ED25519.

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
