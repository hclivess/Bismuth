# doc/23 — HD deterministic-seed wallets & multisig

Status: **HD wallet implemented + tested** (`hd_wallet.py`). Multisig: see §2 below.

Both features are **wallet-side and non-consensus**. A Bismuth address is a hash of a public key, so the
chain neither knows nor cares how the key was derived — these work identically pre- and post-fork, and add
no rules to the node. They build on the in-tree secp256k1 ECDSA signer (`polysign/signer_ecdsa.py`,
`Bis1...` Base58Check addresses), because secp256k1 is what makes both BIP32 derivation and key
aggregation natural; `coincurve` and `base58` are already hard dependencies.

---

## 1. HD wallet (BIP32-style) — a fresh address per payment

`hd_wallet.py`. One seed → unlimited deterministic keypairs → a new `Bis1...` receive address for every
payment, exactly like a Bitcoin HD wallet. Every derived address is a real Bismuth ECDSA address that the
node accepts and that can sign transactions (proven end-to-end on regnet — `test_hd_address_is_spendable`
funds a derived address from the RSA wallet, then spends from it with its derived key).

### Why ECDSA, not the default RSA
BIP32 child derivation is a scalar tweak on the private key — `child = parse256(IL) + k_par (mod n)` — a
one-line operation on secp256k1 and effectively impossible for RSA (whose keygen is an expensive prime
search, not a scalar op). So HD uses the ECDSA signer. RSA wallets keep working unchanged; HD is additive.

### Derivation (BIP32)
```
master:  I = HMAC-SHA512("Bismuth seed", seed); k = I[:32] (must be in 1..n-1), chain = I[32:]
CKDpriv(k_par, c_par, i):
   hardened (i >= 2^31): I = HMAC-SHA512(c_par, 0x00 || ser256(k_par) || ser32(i))
   normal   (i <  2^31): I = HMAC-SHA512(c_par, serP(point(k_par)) || ser32(i))
   k_i = (parse256(I[:32]) + k_par) mod n   — invalid (skip index) if I[:32] >= n or k_i == 0
default account path: m / 44' / coin' / account' / change / index    (BIP44 layout)
```
The seed domain tag is **"Bismuth seed"** (not Bitcoin's "Bitcoin seed") — a deliberately distinct
keyspace, so a Bismuth seed can never collide with a Bitcoin wallet derived from the same entropy.
`coin_type` defaults to `0` and is **not** an official SLIP-44 assignment; it only namespaces accounts
within a wallet. The invalid-key cases (IL ≥ n, or child = 0) are ~2⁻¹²⁷ rare; `addresses()` skips them
per BIP32, and `HDNode` rejects an out-of-range key rather than silently reducing it.

### API
```python
from hd_wallet import HDWallet
w = HDWallet(seed_bytes)                 # or HDWallet.from_passphrase("…")  (PBKDF2-HMAC-SHA512, BIP39 step)
addr   = w.receive_address(0)            # m/44'/0'/0'/0/0   -> "Bis1..."
signer = w.receive_signer(0)             # a polysign SignerECDSA ready to sign
for i, a in w.addresses(20): ...         # the external (receive) chain, fresh address per index
node   = w.master.derive_path("m/44'/0'/3'/0/7")   # arbitrary BIP32 path
```
To spend from a derived address: `hd_wallet.sign_transaction(signer, ts, signer.address(), recipient,
amount, op, openfield)` returns the 8-field tx tuple to `mpinsert` (the test client exposes
`LiteClient.send_with_signer`). The one easy-to-get-wrong detail, centralised in `tx_public_key_b64`: the
tx's `public_key` field for ECDSA/ED25519 is the **raw compressed pubkey bytes, base64-encoded** — NOT the
hex string — because `verify_bis_signature` b64-decodes it and reconstructs the address from those bytes.

### Tests (`tests/test_hd_wallet.py`)
Offline: address validity + factory routing, determinism, per-index/account/change uniqueness, seed
sensitivity, path parsing, child-pubkey consistency, **sign/verify through the real `SignerFactory`**,
wrong-key rejection, passphrase reproducibility, invalid-index skipping, out-of-range key rejection. Live:
fund a derived address and spend from it to another derived address.

### BIP39 mnemonic (`bip39.py`) — the 12/24-word backup
The standard human-readable phrase, implemented to spec so it is portable to/from any BIP39 tool:
`generate(strength)` → a 12–24 word mnemonic; `HDWallet.from_mnemonic(phrase, passphrase="")` restores a
wallet (rejecting a bad checksum); `HDWallet.new_mnemonic()` returns `(phrase, wallet)`. The bundled English
wordlist is the canonical 2048-word list (sha256 `2f5eed53…b24dbda`, re-checked by `bip39.verify_wordlist`).
Correctness is pinned to the official Trezor vectors — entropy→mnemonic→seed reproduced byte-for-byte
(`tests/test_bip39.py`), so a Bismuth seed phrase backs up and restores anywhere. The phrase↔seed mapping is
standard BIP39; Bismuth's BIP32 step still keys the master with `b"Bismuth seed"` (a deliberately distinct
keyspace — §1), so the same phrase gives portable BACKUP but Bismuth-specific ADDRESSES. The older
`from_passphrase` remains as a non-BIP39 brain-wallet option.

### Gap-limit scanning (`HDWallet.scan`)
Address/balance discovery across the chain — walk the receive (and change) chain, stopping after
`gap_limit` (default 20, per BIP44) consecutive UNUSED addresses — so restoring from a seed/mnemonic finds
all funded addresses without scanning forever. See §3 below.

---

## 2. Multisig (M-of-N)

### Two mechanisms, both shipped
Bismuth now offers M-of-N **two** ways, and you pick by trade-off:
- **§2.1 Native signer (base layer, consensus, hf2-gated)** — a real multisig *address type*; the node
  verifies M signatures over the N-key redeem in its own signature path. Leaner and cheaper than a
  contract, and it composes with timelocks/hashlocks for atomic-swap HTLCs (see doc/24). **Implemented.**
- **§2.2 VM-custody vault (app layer, any fork)** — funds custodied by a contract released on M approvals.
  Programmable (add policy, roles, spending limits) and needs **zero** consensus change.

A normal Bismuth tx still verifies **one** signature against the sender address
(`digest_tx.validate → SignerFactory.verify_bis_signature`); the native signer (§2.1) extends exactly that
path with a multisig verifier keyed on the `MAINNET_MULTISIG` / `TESTNET_MULTISIG` address version. The
VM vault below (§2.2) instead locks funds to contract logic.

### 2.1 Native multisig signer (base layer, consensus) — IMPLEMENTED

`polysign/signer_multisig.py` + `multisig_wallet.py`. A real M-of-N **address type**: the sender IS a
multisig address, the spend carries M signatures over the N-pubkey redeem, and the node verifies the
threshold in its own signature path. The on-chain analogue of Bitcoin P2SH multisig, gated to post-hf2.

**Address (P2SH-style).** `redeem = bytes([M, N]) || pub₁(33) || … || pub_N(33)`, the compressed pubkeys
**sorted (BIP67)** so the address depends on the owner SET and threshold, never the listing order. The
address is `base58check(version_multisig || ripemd160(sha256(redeem)))` using the already-reserved
`MAINNET_MULTISIG` (`\x4f\x54\xc8`) / `TESTNET_MULTISIG` versions — a fixed textual prefix **`Bism…`**
(mainnet) / **`mBis…`** (testnet), distinct from regular ECDSA `Bis1…`, so factory routing never collides.

**Wire form (rides the existing 8 tx fields — no format change).**
```
public_key field = base64(redeem)
signature field  = base64( bytes([k]) || (owner_idx, sig_len, DER_sig) × k )   # idx STRICTLY INCREASING
```

**Verification (`SignerMultisig.verify_bis_signature`, the node's reject path).**
1. Parse the redeem → `(M, N, owners)`; **rebuild the address and require it == the sender** (so the
   redeem can't be swapped).
2. Parse the signature list; require `k ≥ M`, indices **strictly increasing and `< N`** (distinct owners,
   canonical order), and **every** provided signature valid over the canonical tx buffer under `owners[idx]`.
   Threshold met iff the valid count ≥ M.

**Fork gating.** The threshold check is pure crypto; the hf2 TIMING rule lives in the digester
(`digest.py`): a block carrying a multisig **sender** at/below `node.fork_height` is rejected (an upgraded
node must never accept a multisig spend a pre-fork node would reject — chain-split safety). Receiving
*into* a multisig address is allowed any time (it is just an address). Inert once activated, exactly like
the shielded / VM gates.

**This directly answers the known native-multisig hazards** (signature ordering, threshold rules, replay,
serialization): ordering is canonicalised by the strictly-increasing index rule (which also kills
reorder-malleability and stops one owner filling two slots); the threshold is counted over *distinct,
all-valid* signatures; replay/tamper fails because the signatures are over the canonical buffer binding
every tx field (and Bismuth's existing duplicate-signature + timestamp-window guards still apply); the
redeem and signature list are length-prefixed and bounds-checked, never trusted for length.

**Honest constraint (a real consensus limit, enforced not truncated).** The tx `signature` field is frozen
at 684 chars and `public_key` at 1068 (`digest_tx.py`). A DER signature is ~71 bytes, so a spend fits a
THRESHOLD of up to ~6 provided signatures; N (owners) up to `MAX_OWNERS = 15`. `serialize_redeem` and
`MultisigAccount.assemble_signature` reject an over-long field **at build time** (a loud error) rather than
letting it be silently truncated into an unverifiable spend. For most real multisig (2-of-3, 3-of-5,
2-of-2) this is ample; a higher threshold needs the deferred items below.

**Wallet (`multisig_wallet.MultisigAccount`).** `from_owners([keys…], M)` → address + redeem;
`partial_sign(buffer, owner)` → one owner's `(idx, sig)`; `assemble_signature([partials])` combines them;
`sign_transaction([≥M owners], …)` builds, combines, self-verifies and returns the 8-field tx. Owners can
be raw secp256k1 keys, `SignerECDSA`, or **HD-derived `hd_wallet.HDNode`s** — so a vault composes with §1's
deterministic-seed keys (each owner derives from its own seed). This is the create / sign / partial-sign /
combine / broadcast flow a multisig wallet needs.

**Tests (`tests/test_multisig_signer.py`).** 14 offline adversarial + 2 live regnet: address routing &
order-independence, every quorum verifies, **threshold-not-met / non-owner-sig / one-owner-two-slots /
out-of-order-index / replay-on-different-message / wrong-redeem / unsorted-redeem rejected**, build
validation, the signature-field size bound, serialize/parse round-trip + corruption, HD-owner composition;
and live: fund a 2-of-3 vault and spend it with two owners post-fork, plus an under-threshold spend the
node refuses.

### Still deferred for the native signer
- **Thresholds beyond the 684-char field** (M ≳ 7) would need either compact (64-byte) signature encoding
  or a base-tx field-size change (a heavier consensus change); the VM vault (§2.2) has no such cap.
- **Nested / script multisig** (multisig-of-multisig, weighted keys, per-key policy) — use the VM vault.
- **HTLC composition** (a hashlock/timelock spend path on top of the redeem) for atomic swaps is tracked
  separately in doc/24.

### 2.2 VM-custody vault (app layer)

`contracts/multisig.py` — an M-of-N vault as a **VM contract**: funds are custodied by the contract and
released only when at least M of the N owners approve a payout (P2SH/P2WSH lock a UTXO to a script
requiring M-of-N; here funds are locked to a contract). It rides the existing hf2 VM like the
escrow/raffle demos, is fully programmable, and needs **zero** consensus change.

#### Model (one standing proposal at a time)
- **DEPOSIT** — anyone funds the vault (`callvalue` accumulates the pot).
- **PROPOSE** — an owner proposes `(recipient, amount)`: stores it, **clears all approvals**, and counts as
  the proposer's own approval. A fresh PROPOSE supersedes any prior one (so stale approvals never carry
  over — regression-tested).
- **APPROVE** — an owner approves the standing proposal (idempotent: one owner = one approval).
- **EXECUTE** — anyone may execute once ≥ M distinct owners approved and the pot covers the amount; it
  marks executed **before** transferring (fail-safe against double-pay) and pays the stored recipient.

Owners are identified by their CALLER fold (low 32 bits of the address), baked into the bytecode at
deploy (`build([owner_id,...], threshold)`), like the escrow's party ids. Approvals are per-owner storage
bits; EXECUTE sums them and compares to the threshold.

#### Tests (`tests/test_multisig.py`)
Offline (drive the bytecode directly): 2-of-3 full lifecycle, non-owner propose/approve rejected,
below-threshold execute rejected (3-of-3 with 2 approvals), double-execute rejected, **approval reset on a
new proposal**, idempotent approval, amount-over-pot rejected, and `build()` parameter validation. Live
regnet: deploy a vault with the wallet as an owner (threshold 1), deposit, propose, execute, and confirm
the custody drains to the payee.

#### Honest limits
- **CALLER folds are 32-bit** (a Bismuth design choice the VM exposes). Two different owner addresses
  could in principle fold to the same 32-bit id; for a vault holding real value, choose owners whose folds
  differ (trivially checkable) — `build()` rejects *exactly* equal owner ids but cannot see the full
  addresses. A production vault should bind full addresses (e.g. hash them) rather than folds.
- **Demo amount cap:** the proposed amount is one 32-bit word (~42.9 BIS in integer units), matching the
  other demos' stored-amount limitation; a production vault carries a 64-bit amount.

#### When to use which
Use the **native signer (§2.1)** for plain M-of-N custody — treasuries, 2-of-3 escrow, shared wallets:
it is cheaper, smaller, and composes with HTLC atomic swaps. Use the **VM vault (§2.2)** when you need
*programmable* policy beyond a pure threshold (roles, spending limits, time windows, dispute flows, or
multisig combined with arbitrary contract logic).

---

## 3. Gap-limit scanning (wallet recovery)

`HDWallet.scan(is_used, gap_limit=20, account, change)` and `HDWallet.scan_used_addresses(...)`. When a
wallet is restored from a seed/mnemonic, the addresses it once handed out aren't recorded anywhere on the
wallet side — they must be rediscovered by walking the derivation chain and asking the chain "was this
address ever used?". The BIP44 **gap limit** bounds that walk: stop after `gap_limit` (default 20)
*consecutive* unused addresses. The gap counter resets on every hit, so a wallet with gaps smaller than
the limit is fully recovered, while an all-empty chain terminates quickly.

`is_used(address) -> bool` is a caller-supplied oracle, so the scanner stays node-agnostic — wire it to
whatever the wallet can see (e.g. `balanceget`, an address's tx history via the REST API, or an explorer).
`scan_used_addresses` sweeps both the external (receive) and internal (change) chains for an account.

Tests (`tests/test_hd_wallet.py`): offline — recover a sparse wallet (`{0,1,4,7,19}`), stop correctly past
the gap (index 25 invisible at limit 20, found at limit 30), empty chain, both chains; live — fund receive
indices 0 and 2 (leaving 1 empty), then rediscover exactly `{0,2}` from the seed via a balance oracle.
