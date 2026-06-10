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

(Implemented next in this branch — see `multisig.py` and `tests/test_multisig.py`; this section is filled
in when that lands.)
