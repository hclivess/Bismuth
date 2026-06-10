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

### Not included (honest scope)
A BIP39 mnemonic **wordlist** (the human 12/24-word phrase) is not bundled — `from_passphrase` does
BIP39's seed-stretching step (PBKDF2) over a raw passphrase, and a wordlist could layer on top unchanged.
Gap-limit scanning / balance discovery across the address chain is a wallet-UX concern left to the wallet.

---

## 2. Multisig (M-of-N)

`contracts/multisig.py` — an M-of-N vault as a **VM contract**: funds are custodied by the contract and
released only when at least M of the N owners approve a payout. This is the postfork analogue of Bitcoin's
script-based multisig (P2SH/P2WSH lock a UTXO to a script requiring M-of-N; here funds are locked to a
contract requiring M-of-N), and it rides the existing hf2 VM like the escrow/raffle demos.

### Why a contract, not a native multisig signer
The node verifies **one** signature per transaction, against the sender address
(`digest_tx.validate → SignerFactory.verify_bis_signature`). Real on-chain M-of-N signature aggregation
would require a new consensus signature path (a new polysign verifier wired into the digester). The
`SignerSubType.MAINNET_MULTISIG` address version exists, but **no verifier implements it** — adding one is
a consensus change and its own gated hard fork. A custody contract delivers M-of-N **today**, on the VM
layer, with zero consensus risk. The native-signer route is the deferred alternative (see §2.1).

### Model (one standing proposal at a time)
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

### Tests (`tests/test_multisig.py`)
Offline (drive the bytecode directly): 2-of-3 full lifecycle, non-owner propose/approve rejected,
below-threshold execute rejected (3-of-3 with 2 approvals), double-execute rejected, **approval reset on a
new proposal**, idempotent approval, amount-over-pot rejected, and `build()` parameter validation. Live
regnet: deploy a vault with the wallet as an owner (threshold 1), deposit, propose, execute, and confirm
the custody drains to the payee.

### Honest limits
- **CALLER folds are 32-bit** (a Bismuth design choice the VM exposes). Two different owner addresses
  could in principle fold to the same 32-bit id; for a vault holding real value, choose owners whose folds
  differ (trivially checkable) — `build()` rejects *exactly* equal owner ids but cannot see the full
  addresses. A production vault should bind full addresses (e.g. hash them) rather than folds.
- **Demo amount cap:** the proposed amount is one 32-bit word (~42.9 BIS in integer units), matching the
  other demos' stored-amount limitation; a production vault carries a 64-bit amount.

### 2.1 Deferred: a native multisig signer (consensus)
A true M-of-N **address type** (sender = multisig address; the tx carries M signatures over N pubkeys; the
node verifies the threshold) would use the already-reserved `MAINNET_MULTISIG`/`TESTNET_MULTISIG` address
versions in `polysign/signer_ecdsa.py`. It needs a new `verify_bis_signature` path that decodes the
concatenated pubkeys+signatures and checks ≥ M valid — a **consensus change**, gated and replay-validated
like any hard fork. Deferred in favour of the VM-contract vault above, which needs no consensus change.
