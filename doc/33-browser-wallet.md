# doc/33 — Bismuth Browser Wallet + Injected Provider (design)

The poker SPA (doc/28 §8.1) and every other dApp demo (`web/amm`, `web/dex`, `web/predictionmarket`,
`web/raffle`, `web/router`, `web/poker`) currently sign **server-side**: a local `relay.py` holds a
`wallet.der` and signs/submits on the browser's behalf, because "the node REST API is read-only and a
browser cannot sign or speak the protocol." That is a demo crutch — the relay is a custodian of the user's
key. This document specifies the replacement: a **client-side wallet whose keys never leave the browser**,
exposed to dApps through a **MetaMask/Kukai-style injected provider**, submitting directly to the node's
write endpoint. The relay is demoted to (a) the off-chain deal **message bus** (doc/28 §5) and (b) an
explicit **dev-only fallback signer**.

This is design only. Implementation is doc/28 Stage 3.

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
 dApp page (poker SPA, amm, …)         window.bismuth (injected provider, EIP-1193-like)
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
| `bismuth_vmCall` | `{contract, calldata_hex, value}` | `{txid}` | yes (high-level helper → builds the `vm:call` tx of §4, then `bismuth_sendTransaction`) |

Events (subscribe via `window.bismuth.on(...)`): `accountsChanged`, `connect`, `disconnect`,
`chainChanged` (network/ledger switch). Provider exposes `isBismuth = true` and `networkId`
(ledger fingerprint, so a dApp can refuse a wrong-chain wallet).

**Permissioning:** the wallet keeps a per-dApp-origin allowlist (connected? which methods auto-approved?).
First `requestAccounts` from an origin shows a connect prompt; signing always prompts unless the user ticked
"don't ask again for this contract" (and even then, value-bearing calls — i.e. `FN_SIT`/`FN_REGISTER` — always
prompt, because they move funds).

## 4. Transaction model + submission

A Bismuth tx is the tuple the node validates (`rest_api.py` write body fields):
`[timestamp, address(sender), recipient, amount, signature, public_key, operation, openfield]`.

A **contract call** is `operation="vm:call"`, `openfield="<contractAddr>:<calldata_hex>"`, `recipient =
contractAddr`, `amount = value` (BIS attached — nonzero only for `FN_SIT`/`FN_REGISTER`; every other selector
runs `_require_no_value`). `calldata = be4(selector) ++ args` exactly as the contracts decode it.

**Signing** reuses the node's existing scheme (whatever `mempool.merge` checks today): the wallet builds the
canonical tuple, signs the digest with the account key, attaches `signature`+`public_key`, and POSTs to
`rest_api_write`'s `POST /api/transaction`. That endpoint runs the **same** signature/balance/dup/format
validation as the socket `mpinsert` — a new transport, **not** a new rule (`rest_api.py` header). The wallet
lets the user pick/recall a node URL; reads (balance, `GET /api/vm/contract/<addr>`, mempool, height) also go
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
  (e.g. e2e-encrypting a `holepart` to a victim in the deal, doc/28 §5) get a *signature/derived pubkey*,
  not the key.

## 6. dApp integration (what changes)
* **Poker SPA** (`web/poker/`, rebuilt in Stage 3): replace every `POST /relay/<action>` server-sign with
  `window.bismuth.bismuth_vmCall({contract, calldata_hex, value})`. The SPA builds `calldata` for
  `FN_SIT/DEAL/COMMIT/CHECK/CALL/BET/FOLD/REVEAL/TIMEOUT/SETTLE/DECK_DIGEST` itself (it already knows the ABI
  from `poker_table.py`). The **deal** runs over the relay bus (`/relay/msg`+`/inbox`) using the **JS port of
  `contracts/poker_deal.py`** — byte-for-byte, including the **true 64-hex secp256k1 prime** (the current
  `index.html` literal is a truncated non-prime; see doc/28 §5 note) and the canonical `_deck_bytes`/
  `cardmap_bytes` serialization so `FN_DECK_DIGEST` hashes match the contract's stored anchors.
* **Other demos:** the same `connect` button now connects the real wallet; `relay.py` keeps only `/relay/msg`
  + `/inbox` (+ an optional `--dev-sign` flag for headless CI).

## 7. The relay's reduced role
`relay.py` becomes: **(a) message bus** for the off-chain deal (carries `deckN`/`lock`/`holepart`/`boardpart`
messages between players; learns nothing — keys never transit it, holeparts are e2e-encrypted to the victim);
**(b) dev fallback signer** behind an explicit `--dev-sign wallet.der` flag for automated/regnet tests, with a
loud banner that it is custodial and not for real funds. Default (no flag) = bus only.

## 8. Security model + threats
1. **Origin isolation** — wallet app on its own origin/sandboxed iframe; `postMessage` origin-checked both
   ways; dApp page never in the same JS realm as the keys.
2. **Per-call approval** — human-readable decode of the `vm:call` (which contract, which selector, how much
   value) in the modal; value-bearing calls always prompt.
3. **Phishing/clickjacking** — approval modal frame-busts; shows the connected dApp origin; warns on a new
   contract address.
4. **Wrong chain** — `networkId` fingerprint; dApp and wallet both refuse a mismatch (avoids replaying a
   regnet-signed tx onto mainnet, cf. the fork-lockin pollution lesson).
5. **Deal secrecy** — provider exposes only `signMessage`/derived pubkeys, never the key; the deal's
   per-position keys are ephemeral per hand and live in the wallet/deal worker, not the dApp page.
6. **At-rest** — KDF-stretched passphrase + AES-GCM; idle auto-lock; no seed in `localStorage`/cookies.
7. **Demo-grade flags** — the SRA prime/encoding and the `--dev-sign` relay are explicitly not-for-real-money
   (carried over from doc/28 §9 [SEC-H1]).

## 9. Build plan (Stage 3)
| step | artifact | verifiable headless? |
|---|---|---|
| 1 | `web/lib/bismuth-provider.js` (injected shim) + `web/lib/bismuth-deal.js` (JS port of `poker_deal.py`) | **yes** — `node` cross-check the deal JS against `tests/test_poker_deal.py` vectors (same prime, enc/dec, card map, `deal_digests`) |
| 2 | `web/wallet/` (key vault + approval UI + node submit) | partial — unit-test signing/tx-build against a known tx; UI manual |
| 3 | `web/poker/` rebuilt SPA (graphical table, seats, pot, betting controls, deal flow, showdown) using the provider | manual/browser; ABI calldata builders unit-tested vs `poker_table.py` selectors |
| 4 | trim `relay.py` to bus + `--dev-sign`; keep a regnet end-to-end (dev-sign) smoke | **yes** (regnet) |

## 10. Open questions
* Exact node signing primitive to mirror in JS (classic RSA-PKCS1 vs the modern secp256k1 path the
  shielded/HD work uses) — pin against `mempool.merge` before writing the JS signer.
* Extension packaging (MV3) vs in-page iframe for production.
* Multi-account UX in the approval modal; hardware-wallet support later.
