# doc/33 — Bismuth Browser Wallet + Injected Provider (design)

A **client-side wallet whose keys never leave the browser**, exposed to web pages through a
**MetaMask/Kukai-style injected provider** (`window.bismuth`), submitting signed transactions directly to
a node's write endpoint. This replaces the older server-side-signing pattern, where a local relay held a
`wallet.der` and signed on the browser's behalf — i.e. was a custodian of the user's key.

> **Scope note.** This design was originally motivated by the dApp demos that rode the contract VM. The
> VM and every dApp built on it have since been removed; the wallet and the provider survive, and now
> cover plain value/data transactions (transfers, `token:`/`alias=` operations, message signing).

## 1. Goals / non-goals
* **Keys never leave the browser.** No relay, server, or page script ever sees a private key or seed.
* **Two products, one model** (like Kukai + Beacon, or MetaMask's wallet + EIP-1193): a **web wallet**
  (the key vault + signing UI) and an **injected provider** (`window.bismuth`) any dApp page can call.
* **Per-call user approval.** Every signature is an explicit, human-readable approval prompt — never a
  blanket "the relay does whatever."
* **Submit over HTTP.** Sign in-browser, POST the signed tx to a node's `rest_api_write` endpoint; no new
  consensus rule, just a transport (see §4).
* **Reuse existing crypto.** BIP39 mnemonic + `hd_wallet.py` derivation; the same tx signing the node
  already validates via `mempool.merge`.
* **Non-goals:** a browser extension binary (we ship an in-page provider + an optional bookmarklet/SDK;
  an extension is a later packaging step); custodial recovery; fiat on-ramp.

## 2. Architecture — two layers

```
 web page                              window.bismuth (injected provider, EIP-1193-like)
        │  provider.request({method,params})        │
        ▼                                            ▼
 ┌─────────────────────────┐   postMessage    ┌──────────────────────────────────────┐
 │ Provider shim (in page) │◄────────────────►│ Wallet app (separate origin / iframe)│
 │  - no keys, just RPC     │   (origin-checked)│  - BIP39 seed (encrypted, IndexedDB) │
 │  - dApp-friendly API     │                   │  - hd_wallet derivation, signing      │
 └─────────────────────────┘                    │  - approval UI (modal)                │
                                                 │  - submits to node POST /api/transaction
                                                 └──────────────────────────────────────┘
```

* **Provider shim** (`web/lib/bismuth-provider.js`): tiny, keyless. Injected as `window.bismuth`. Talks to
  the wallet app over `window.postMessage` with strict `origin` checks (the wallet app lives on its own
  origin / sandboxed iframe so a malicious dApp cannot read its DOM or storage). Mirrors MetaMask's
  `request`/events shape so the mental model is familiar.
* **Wallet app** (`web/wallet/`): the Kukai analog. Owns the seed, the derivation, the approval modal, and
  the node connection. The **only** code that touches private key material.

Single-machine demo simplification: the wallet app may run as a sandboxed `<iframe>` from a second local
origin/port; production would be a separate tab or an extension. The provider↔wallet protocol is identical
either way.

## 3. The injected provider API (`window.bismuth`)

EIP-1193-flavoured so it reads like MetaMask, but Bismuth-native methods:

| method | params | returns | approval? |
|---|---|---|---|
| `bismuth_requestAccounts` | — | `[address]` | yes (connect) |
| `bismuth_accounts` | — | `[address]` (empty if not connected) | no |
| `bismuth_getBalance` | `{address}` | `{balance, …}` (read via node REST) | no |
| `bismuth_signTransaction` | `tx` (see §4) | `{…tx, signature, public_key}` | yes |
| `bismuth_sendTransaction` | `tx` | `{txid}` (sign **and** POST to node) | yes |
| `bismuth_signMessage` | `{message}` | `{signature, public_key}` | yes |

Events (subscribe via `window.bismuth.on(...)`): `accountsChanged`, `connect`, `disconnect`,
`chainChanged` (network/ledger switch). Provider exposes `isBismuth = true` and `networkId`
(ledger fingerprint, so a dApp can refuse a wrong-chain wallet).

**Permissioning:** the wallet keeps a per-origin allowlist (connected? which methods auto-approved?).
First `requestAccounts` from an origin shows a connect prompt; signing always prompts, and any transaction
that moves funds always prompts.

## 4. Transaction model + submission

A Bismuth tx is the tuple the node validates (`rest_api.py` write body fields):
`[timestamp, address(sender), recipient, amount, signature, public_key, operation, openfield]`.

`operation`/`openfield` carry whatever the caller supplies — empty for a plain transfer, or a
`token:transfer` / `alias=` style operation with its data.

**Signing** reuses the node's existing scheme (whatever `mempool.merge` checks today): the wallet builds the
canonical tuple, signs the digest with the account key, attaches `signature`+`public_key`, and POSTs to
`rest_api_write`'s `POST /api/transaction`. That endpoint runs the **same** signature/balance/dup/format
validation as the socket `mpinsert` — a new transport, **not** a new rule (`rest_api.py` header). The wallet
lets the user pick/recall a node URL; reads (balance, mempool, height) also go
through node REST.

Nonce/dup: Bismuth dedups on the signature/tx content, so the wallet must vary the timestamp and surface
"already in mempool" so a dApp can poll for inclusion (mirrors the live-regnet mempool-contention gotcha).

## 5. Key management
* **Seed:** BIP39 mnemonic (reuse the Stage-`deferred` BIP39 + `hd_wallet.py` gap-limit derivation). Generated
  in-browser with `crypto.getRandomValues`; shown once for backup.
* **At rest:** seed encrypted (passphrase → Argon2/scrypt KDF → AES-GCM) in `IndexedDB` of the **wallet
  origin only**. Unlocked into memory for a session; auto-locks on idle.
* **Accounts:** HD-derived; the active account's address is what `bismuth_accounts` returns. Watch-only import
  (address, no key) for spectators (doc/28 §6 betting can be address-keyed).
* **Never exported:** no API returns the seed/private key. `signMessage` exists so dApps that need a key
  get a *signature/derived pubkey*, not the key.

## 8. Security model + threats
1. **Origin isolation** — wallet app on its own origin/sandboxed iframe; `postMessage` origin-checked both
   ways; dApp page never in the same JS realm as the keys.
2. **Per-call approval** — human-readable decode of the transaction (recipient, amount, operation, data)
   in the modal; value-bearing transactions always prompt.
3. **Phishing/clickjacking** — approval modal frame-busts and shows the connected page origin.
4. **Wrong chain** — `networkId` fingerprint; dApp and wallet both refuse a mismatch (avoids replaying a
   regnet-signed tx onto mainnet, cf. the fork-lockin pollution lesson).
5. **Key secrecy** — the provider exposes only `signMessage`/derived pubkeys, never the key itself.
6. **At-rest** — KDF-stretched passphrase + AES-GCM; idle auto-lock; no seed in `localStorage`/cookies.

## 9. Verification

`web/lib/_crosscheck/` cross-checks the JS signer against the node's own verifier headlessly: generate a
throwaway RSA wallet, JS-sign a transaction with `bismuth-tx.js`, and have the node's real
`SignerFactory.verify_tx_signature` accept it (`run_crosscheck.sh`). No ledger, node service, or network.

## 10. Open questions
* Extension packaging (MV3) vs in-page iframe for production.
* Multi-account UX in the approval modal; hardware-wallet support later.
