# doc/22 — Shielded value (core, post-fork)

Status: **stage 1 + stage 2 implemented** (this branch). Stage 3 (RingCT) deferred — see §9.
Gated on: **hf2** (`node.fork_height`) — the same activation as the VM (doc/19). Inert until then.

> **Stage 2 (ring signatures) is live.** Spends/redeems now hide *which* output is consumed behind a
> CryptoNote ring signature, and the spent-set is a set of **key images** (not per-note nullifiers).
> The model shift this forces is described in §12; read it before §6/§7, which describe the stage-1
> nullifier model that §12 supersedes.

This is the in-core successor to the off-chain `shielded-tokens` prototype
(github.com/bismuthfoundation/shielded-tokens). It keeps that project's goal — move value through a
privacy pool carried in the `operation`/`openfield` of ordinary Bismuth transactions — but fixes the
properties that made the prototype unsafe as money, by validating the pool **in consensus** instead of
in a per-user sidecar app.

PoW, block format, and the signature/balance rules for transparent transactions are **unchanged**.
Mining is untouched: shielded transactions are ordinary signed transactions with a known `operation`,
and the extra validation runs in the digest path next to the token/VM logic, not in the miner.

---

## 1. Why not just ship the prototype

The prototype (`tokens_shielded.py`) has four disqualifying flaws for a core feature:

1. **One shared symmetric key per token.** Every holder shares one AES-256 key, so any holder can
   decrypt *every* transfer of that token. There is no privacy between holders — only from outsiders.
2. **No consensus.** Balances live in per-user `shielded_accounts/*.json`, recomputed by each client.
   Nothing on-chain is validated; two clients can disagree on who owns what and the chain can't say
   who is right.
3. **Double-spend "prevention" is a local file.** `shielded_history/<hash>` is a local marker. There
   is no network-wide spent set; the same note can be spent repeatedly and different observers accept
   different histories.
4. **Discovery by shared "signal" strings.** Random `operation` strings, distributed inside the
   keyfile, only obfuscate; they are not unlinkability.

The fix for all four is the same: **per-recipient one-time keys** (so there is no shared key) and a
**consensus-enforced nullifier set** (so the network, not a local file, prevents double-spends).

---

## 2. What stage 1 provides (and does not)

Provides:
- **Recipient privacy / output unlinkability.** Each output pays a fresh one-time key derived by
  Diffie–Hellman; an observer cannot link an output to the recipient's published shielded address, nor
  link two outputs to the same recipient. (CryptoNote stealth addresses.)
- **Consensus double-spend prevention.** Spending a note publishes a unique *nullifier*; every node
  rejects a block that reuses a nullifier. Reorg-safe.
- **Auditable supply.** Amounts are transparent in stage 1, and every unit in the pool is backed 1:1
  by real BIS held at a canonical sink address. `pool_value == ledger_balance(SHIELD_SINK)` is an
  invariant anyone can check. This is the deliberate trade we chose over hidden amounts (see §9).

Does **not** provide (yet):
- **Sender ambiguity.** A stage-1 spend reveals which output it consumes. The output is still
  unlinkable to the recipient's address, but the *spend* is linkable to the *mint*. Ring signatures
  (stage 2) remove this.
- **Amount hiding.** Amounts are public (stage 3 / RingCT, deferred — see §9).

So stage 1 ≈ "stealth addresses + transparent amounts + on-chain nullifiers". Stage 2 adds ring
signatures to reach the CryptoNote (pre-RingCT) privacy level.

---

## 3. Operations

Three on-chain operations (the `shield:` namespace; coexists with `vm:` and `token:`):

| operation        | transparent effect                         | shielded effect                              |
|------------------|--------------------------------------------|----------------------------------------------|
| `shield:mint`    | sender pays `amount` to `SHIELD_SINK`      | creates one shielded note for a recipient    |
| `shield:spend`   | none (fee only)                            | consumes 1 note, creates N notes (Σ = in)    |
| `shield:redeem`  | consensus pays `amount` SINK→recipient     | consumes 1 note                              |

A shielded address (`shield:addr`) is **off-chain** — it is published/handed out like any address and
never appears on chain except as the implicit target of the DH derivation.

`SHIELD_SINK` is a fixed, unspendable-by-key sink address (no known private key); it can only be
debited by the consensus-generated redeem payout. This is the exact pattern VM custody uses
(`vm_engine.VM_SINK`, doc/19).

---

## 4. Cryptography (secp256k1, via `coincurve`)

We use the curve Bismuth already ships (`coincurve` = libsecp256k1 bindings, a hard dependency). No new
crypto library, no hand-rolled field arithmetic. Stealth addresses are curve-agnostic; secp256k1 is
chosen purely because it is the vetted primitive already present (pynacl/ed25519 hash-to-point is not
available here).

A shielded address is two public keys:
- **scan key** `A = a·G` — used to *detect* incoming notes (a "view key").
- **spend key** `B = b·G` — used to *spend* them.

Published address = `shield1 || hex(A_compressed) || hex(B_compressed)` (33+33 bytes).
Separating scan from spend lets a holder delegate detection (hand out `a`) without handing out spend
authority (`b`) — the standard view-key pattern.

### Mint / output derivation (sender knows A, B; not a, b)
```
r        = random scalar            ;  R = r·G          (ephemeral pubkey, published in the note)
ss       = SHA256("bis-shield-ss/v1" || ECDH(r, A))     (ECDH(r,A) = ECDH(a,R), see below)
ot       = SHA256("bis-shield-ot/v1" || ss)  mod n      (one-time offset scalar)
P        = B + ot·G                                      (one-time output pubkey — who the note pays)
note_id  = SHA256("bis-shield-note/v1" || P)
memo     = AES-256-GCM(key = SHA256("bis-shield-memo/v1" || ss), plaintext = {amount, token, ...})
```
`ECDH(r,A) == ECDH(a,R)` because `r·A = r·a·G = a·R`. Sender computes the left, recipient the right;
both get the same `ss` without sharing a secret. The per-output `ss` (not a shared per-token key) is the
core fix for prototype flaw #1.

### Detection (recipient knows a, b)
```
for each on-chain note with ephemeral R:
    ss = SHA256("bis-shield-ss/v1" || ECDH(a, R))
    ot = SHA256("bis-shield-ot/v1" || ss) mod n
    if (B + ot·G) == note.P:   the note is mine
        p  = (b + ot) mod n        # one-time PRIVATE key; P = p·G
        plaintext = AES-GCM-open(SHA256("bis-shield-memo/v1" || ss), note.memo)
```
Only the holder of `b` recovers `p` and can spend. A holder of only the scan key `a` can detect but not
spend.

### Nullifier and ownership proof (stage 1)
Spending note `(p, P)` publishes:
```
nullifier = SHA256("bis-shield-nf/v1" || P)
sig       = ECDSA_p( SHA256("bis-shield-spend/v1" || note_id || canonical(outputs|redeem)) )
```
- The nullifier is a deterministic function of the spent output, so a second spend produces the **same**
  nullifier → the consensus set rejects it. (Prototype flaw #3 fixed: the spent set is the chain, not a
  local file.)
- The signature is verified against `P` (recovered from the referenced note). Only the owner of `p` can
  produce it, and it commits to the outputs, so a pending spend can't be re-pointed to steal funds.

> **Stage-2 migration note.** Stage 1's nullifier reveals `P`, so the spend is linkable to that specific
> output. Stage 2 replaces it with the CryptoNote **key image** `I = p · H_p(P)` (where `H_p` is
> hash-to-point) plus a ring signature over a decoy set `{P_1..P_m}`; the key image stays unique per
> output (double-spend protection preserved) while the ring hides *which* output was spent.
> `coincurve` exposes `PublicKey.multiply`/`combine_keys`, which cover the ring/key-image math; the one
> missing primitive is a constant-time hash-to-point on secp256k1, which stage 2 must add (or switch the
> shielded curve to ed25519 with a vetted `crypto_core_ed25519_*` binding). The on-chain note/nullifier
> tables and the consensus hook do **not** change shape for stage 2 — only the contents of the proof and
> the meaning of `nullifier` (→ key image) do.

---

## 5. On-chain encoding

`openfield` carries compact JSON (well under the 100 000-byte cap; `operation` ≤ 30 bytes).

`shield:mint` — transparent `recipient = SHIELD_SINK`, `amount = note_amount`:
```json
{"v":1,"R":"<66 hex>","P":"<66 hex>","amt":<int units>,"tok":"bis","memo":"<b64>","c":"<commitment>"}
```

`shield:spend` — transparent amount 0:
```json
{"v":1,"in":"<note_id>","P":"<66 hex>","nf":"<nullifier>","sig":"<hex>",
 "out":[{"R":..,"P":..,"amt":..,"tok":..,"memo":..,"c":..}, ...]}
```

`shield:redeem` — consensus pays out SINK→`to`:
```json
{"v":1,"in":"<note_id>","P":"<66 hex>","nf":"<nullifier>","sig":"<hex>","to":"<address>","amt":<int>}
```

---

## 6. Consensus rules (the digest path)

Validation runs in `digest.process_block_data` **before `to_db`**, so a violating block is rejected and
never committed (same placement as the VM state-root check). Enforced only when
`block_height >= node.fork_height` (pre-fork these ops are inert data, exactly like `vm:`).

A block is **invalid** if any `shield:` tx in it fails:

- **mint**: malformed; `note_id` already exists; transparent `recipient != SHIELD_SINK`; transparent
  `amount != amt`.
- **spend/redeem**: malformed; referenced input note does not exist (as of height−1) or is already
  spent; `nullifier` already in the consensus set, or repeated earlier in the *same* block; ownership
  `sig` invalid under the input note's `P`; for spend, `Σ out.amt != in.amt`; for redeem, `amt != in.amt`.

Notes:
- **One-block maturity:** a note can be spent only in a block *after* the one that mints it (the input
  must exist in committed state). Keeps intra-block ordering trivial; documented wallet-side.
- **Determinism:** redeem payouts and all state transitions are pure functions of on-chain data, so every
  node computes them identically — no committed state root is required for correctness in stage 1
  (a committed `shield_state_root` in the coinbase, like the VM's, is an optional hardening listed in §9).

After `to_db`, the parsed ops are applied to the sidecar (notes inserted, nullifiers recorded) and each
redeem writes a consensus payout row `SHIELD_SINK → to` via `db_handler.shield_payout` (a negative-height
mirror row, exactly like `vm_payout`/dev rewards), so the sink's ledger balance tracks the pool and rolls
back with the chain.

**Supply invariant:** `Σ unspent note.amt == ledger_balance(SHIELD_SINK)` at every height. Mints credit
the pool and the sink together; redeems debit both; spends conserve. Because amounts are transparent,
this is publicly auditable — there is no path to undetected inflation (contrast §9).

---

## 7. Sidecar state (per-ledger, reorg-safe)

A SQLite sidecar next to the ledger, **namespaced by ledger filename** to avoid the regnet→mainnet
pollution class of bug (doc/18 incident): `shielded-<ledger file>.db`.

```
notes(note_id TEXT PK, create_height INT, token TEXT, amount INT,
      r_pub TEXT, p_pub TEXT, memo TEXT, commitment TEXT)
nullifiers(nullifier TEXT PK, spend_height INT, note_id TEXT)
```
"Spent" is **not** a mutable flag on the note — it is the existence of a `nullifiers` row referencing it.
This makes rollback a pure delete:
```
rollback_under(H):  DELETE FROM nullifiers WHERE spend_height >= H;
                    DELETE FROM notes      WHERE create_height >= H;
```
Wired into **every** rollback site that already rolls the token index — `chain_ops.rollback`,
`chain_ops.blocknf`, `chain_ops.sequencing_check` — so a reorg can never desync the spent set from the
ledger (deleting the nullifier rows makes the notes spendable again on the new branch, which is correct).
The sidecar is a deterministic projection of the chain and can also be rebuilt from scratch by replaying
post-fork `shield:` txs (used on first sync; the same idea as VM-state rebuild).

---

## 8. Fees, config, API

- **Fee:** `shield:` txs pay a surcharge on top of the openfield-size fee (`essentials.fee_calculate`),
  alongside the existing `vm:`/`token:issue` surcharges. Tunable; the point is anti-spam, since shielded
  txs are larger and cost more to validate.
- **Config:** `shield=True` (off by default; `options.py`), mirroring `vm`. The sidecar opens at startup
  when enabled; inert until hf2 activates.
- **API (read-only):**
  - `GET /api/shield/stats` → `{enabled, fork_height, notes, nullifiers, pool_units}`
  - `GET /api/shield/note/{note_id}` → public note fields (never anything decryptable without keys)
  Wallets *scan* by pulling `shield:` txs from the existing `/api/address/.../transactions` /
  block endpoints and trial-decrypting locally; the node never holds view keys.

---

## 9. Deliberately deferred / out of scope (and why)

- **Confidential amounts (RingCT / stage 3): deferred, possibly permanently.** Hidden amounts mean a
  bug in the range proofs is *silent supply inflation* — both Monero (2017) and Zcash (2018) shipped
  counterfeiting-class bugs here. On a small chain with a small team, transparent + auditable supply is
  the safer money. Stage 1's `pool_value == balance(SINK)` invariant is the concrete benefit we keep by
  not hiding amounts.
- **Ring signatures (stage 2): IMPLEMENTED — see §12.** secp256k1 hash-to-point is done by
  try-and-increment, which is variable-time but only over PUBLIC ring keys, so it leaks nothing secret
  (the "constant-time" worry in earlier drafts was misplaced for this use). secp256k1's cofactor-1 makes
  it cleaner than ed25519 (no subgroup checks).
- **Committed `shield_state_root` in coinbase:** optional hardening to catch implementation divergence at
  block time (like the VM root). Stage 1 relies on deterministic recomputation instead; add it if/when
  the pool holds meaningful value.
- **Mempool nullifier dedup:** stage 1 rejects double-spends at block-validation time (consensus). A
  mempool-level pre-check (reject conflicting spends before mining) is a spam/UX improvement, not a
  consensus requirement; left for follow-up.

## 10. Positioning caveats (non-engineering, but load-bearing)

- **Opt-in privacy ⇒ small anonymity sets.** Few users in the pool ⇒ weak privacy in practice. Mandatory
  privacy is stronger but invites exchange delistings and MiCA-style regulatory exposure. This is a
  product/positioning decision, not a code default — stage 1 ships opt-in (`shield=False`).
- **Performance.** secp256k1 ops via libsecp256k1 are fast; the per-block cost is proportional to the
  number of `shield:` txs and is paid by validators. Ring signatures (stage 2) raise per-spend cost with
  ring size — budget for it when sizing fees.

---

## 11. Test coverage

`tests/test_shielded.py` (regnet, `shield=True`, `vm=True`, post-fork) proves end-to-end:
mint → recipient detection (trial-decrypt) → **ring spend** (consumes one note hidden in a ring, value
conserved) → **double-spend rejected by consensus** (a block reusing a key image does not advance the
chain) → **reorg clears the key image** (rollback makes the note spendable again) → redeem round-trips
value back to a transparent address; pool/sink move in lockstep (delta-checked) throughout.
`tests/test_ring_signature.py` unit-tests the ring sig itself (valid verifies; tampered message, swapped
key image, tampered response, and outsider-forgery all fail; key images link same-signer/differ across
signers).

---

## 12. Stage 2 — ring signatures (supersedes §4–§7 for spends)

Stage 1 hid the recipient; stage 2 hides the **sender**. A spend no longer names the note it consumes —
it names a **ring** of same-amount notes and proves, with a CryptoNote linkable ring signature, that it
owns *one* of them, without revealing which.

### What changes
- **Key image replaces the per-note nullifier.** Spending the one-time key `(p, P)` publishes
  `I = p · H_p(P)` (a curve point). `I` is deterministic in the spent note, so a second spend of the same
  note yields the same `I` → rejected. But `I` is **unlinkable to `P`** without `p`, so it does not reveal
  which ring member was spent. The ring signature mathematically forces `I` to be the true key image of
  the actual signer (a forged `I` fails verification), so consensus can trust it.
- **The spent-set is a set of key images, NOT marked notes.** This is the load-bearing shift: consensus
  **cannot** tell which notes are spent (that's the anonymity). So the stage-1 "is this note spent?" query
  is gone, and `notes` becomes purely the decoy/scanning set.
- **Pool accounting moves to flows.** Since "unspent notes" is no longer computable on-chain, the pool
  value is tracked as `Σ flows`, where a mint writes `+A` and a redeem writes `−A` (spends are value-
  neutral and write nothing). This still equals `ledger_balance(SHIELD_SINK)` exactly and still rolls back
  by height — the supply invariant from §6 holds in aggregate, just not per-note.

### Same-amount ring rule (transparent amounts ⇒ denominations)
With transparent amounts, a ring only hides the spender if **all ring members share one amount** —
otherwise the amount of the outputs reveals which member was spent. Consensus therefore **requires every
ring member to have the same amount `A` and token**, and conservation is `Σ out.amt == A` (spend) or
`amt == A` (redeem). The practical consequence is CryptoNote-style **denominations**: you can only ring
with notes of an equal amount, so real anonymity needs a healthy population of equal-amount notes. Full
amount-hiding (so any amounts can ring together) is exactly what RingCT/stage 3 would add, and remains
deferred for supply-audit safety.

### On-chain encoding (v2 spend/redeem)
```json
shield:spend  {"v":2,"ring":["<note_id>",...],"I":"<66 hex>","c":["<64 hex>",...],"r":["<64 hex>",...],
               "out":[{"R":..,"P":..,"amt":..,"tok":..,"memo":..,"c":..}, ...]}
shield:redeem {"v":2,"ring":["<note_id>",...],"I":"<66 hex>","c":[...],"r":[...],"to":"<addr>","amt":<int>}
```
`c`/`r` are the ring signature's per-member challenge/response scalars (`n` of each). Ring size is bounded
`1 ≤ n ≤ MAX_RING` (n=1 is legal but gives no anonymity — it names the note). The signed message binds the
ring ids, the key image, and the outputs/redeem-target, so a pending spend can't be re-pointed.

### Consensus rules (v2 spend/redeem), in `validate_block` before `to_db`
A block is invalid if any spend/redeem: references a ring member that doesn't exist; has non-uniform ring
amount/token; repeats a ring member; has `1 > n` or `n > MAX_RING`; presents a key image already in the
set (cross-block) or repeated in-block; fails `ring_verify`; or breaks conservation (`Σout != A` / `amt
!= A`). Mints are unchanged from stage 1.

### Sidecar (revised schema)
```
notes(note_id PK, create_height, token, amount, r_pub, p_pub, memo, commitment)   -- decoys + scanning
keyimages(image TEXT PK, spend_height INT)                                         -- the spent-set
flows(rowid, height INT, delta INT)                                               -- pool = Σ delta
rollback_under(H): DELETE FROM keyimages WHERE spend_height>=H; notes WHERE create_height>=H;
                   flows WHERE height>=H;
```
Rollback stays a pure height-delete at the same three sites; a reorg removes the key images of undone
spends, making those notes spendable again on the new branch.

### Cryptography (`shieldedv1.py`)
`hash_to_point` (try-and-increment on secp256k1; lift a hashed x-coord to a point, increment on
non-residue — iterations depend only on the public key, so timing leaks nothing). `key_image(p,P)=p·Hp(P)`
via `PublicKey.multiply`. `ring_sign`/`ring_verify` are the CryptoNote sum-of-challenges construction:
`L_i=r_iG+c_iP_i`, `R_i=r_iHp(P_i)+c_iI`, accept iff `Σc_i == H_s(m, L_0,R_0,…)`. All point ops are
libsecp256k1 (`from_valid_secret`, `multiply`, `combine_keys`); negligible-probability identity results
fail safe (verify returns false). Verified adversarially before integration (see `test_ring_signature.py`).
