# doc/28 — On-chain poker: heads-up Texas Hold'em on the VM

A decentralized poker dApp on the Bismuth in-core VM (doc/19): `contracts/poker.py`, a localhost relay +
single-page web client in `web/poker/`, and `tests/test_poker.py`. The chain is the **trustless referee**;
the genuinely hard part — dealing **private** cards with no trusted dealer — runs off-chain as *mental
poker*, with the chain anchoring the commitments.

| Piece | Where | Status |
|-------|-------|--------|
| Table contract (escrow, commit-reveal, on-chain hand eval, settlement) | `contracts/poker.py` | **implemented + tested** |
| Off-chain mental-poker deal (commutative-cipher shuffle) + web UI | `web/poker/` | **implemented** (relay + SPA) |
| Tests (Python-reference evaluator, full flow, live deploy/escrow) | `tests/test_poker.py` | **passing** |

---

## 1. The problem: private cards with no trusted dealer

Real poker needs each player to see only their own hole cards, with a provably fair shuffle and no party
(not even a "dealer") able to peek or cheat. On a blockchain VM this is hard:
- The VM is a **pure function** — no randomness syscall (block height is grindable), so the shuffle's entropy
  must come from the players (commit-reveal) and the deal must hide cards cryptographically.
- The VM is RV32I and **cannot** do the modular-exponentiation crypto of a real shuffle cheaply, and secret
  keys can **never** be on-chain.

So the deal itself runs **off-chain**, in the two players' browsers, as **mental poker** (Shamir–Rivest–
Adleman commutative encryption): each player encrypts the shuffled deck with a secret key; because the cipher
commutes, cards can be dealt by selective decryption so each player sees only their own hole cards and the
shared board, and no single party ever knows the whole deck. The chain's job is to make that deal *binding*
and to settle the result trustlessly.

## 2. What the chain guarantees (`contracts/poker.py`)

Heads-up, **all-in** Texas Hold'em (each player stakes a buy-in, then plays or folds; if both are in, the
board runs out and the showdown is evaluated on-chain). The contract is the referee:
- **Escrow** — both buy-ins live in the contract's VM custody (doc/19); the pot can only leave to a player.
- **Commitments** — each player posts `SHA256(hole0 | hole1 | nonce)` *before* acting, so the hand is fixed.
- **Betting** — a bounded state machine: `STAKE0 → JOIN → COMMIT×2 → P0 PLAY/FOLD → P1 CALL/FOLD → BOARD →
  REVEAL×2 → settle`, with a `SYS_NUMBER` block-height **timeout** so a vanishing opponent forfeits.
- **Showdown** — each player reveals `(hole0, hole1, nonce)` plus **5 indices** into their seven cards
  `[hole0, hole1, board0..4]`; the contract checks the reveal against the commitment, that the five indices
  are distinct and in range, then runs an **on-chain 5-card hand evaluator** and pays the better hand (split
  on tie). Choosing indices (not arbitrary cards) makes "best five of seven" trustless without a 21-combo
  search in assembly — a player can only ever play cards they actually hold, and picking a worse five only
  hurts themselves.

### The on-chain hand evaluator (the meaty trustless part)
`_rank5` computes a single comparable integer `category<<20 | tiebreak` for five cards (`card = 0..51`,
`rank = card%13`, `suit = card//13`). It is **count-based**: it builds a 13-entry rank-count array, detects a
flush and the highest straight (including the `A-2-3-4-5` wheel), classifies from the multiplicity multiset,
and packs a tiebreak that orders ranks by **(count desc, rank desc)** — so a pair/trips/quad rank dominates
the kickers (a naive sorted-rank tiebreak gets this wrong). `tests/test_poker.py` recomputes the exact same
integer in Python (`rank5_ref`) and checks the contract against it for **every category**, the category
ordering, and several same-category tiebreaks (wheel, count-ordered kickers, higher-pair-wins).

### ABI (selector = first 4 calldata bytes)
```
FN_STAKE0(0)  addr(28)            +value   P0 stakes the buy-in + records its payout address
FN_JOIN(1)    addr(28)            +value   P1 stakes the buy-in + records its payout address
FN_COMMIT(2)  commit(32)                   caller posts SHA256(hole0|hole1|nonce)
FN_PLAY(3)                                 P0 stays (button) / P1 calls
FN_FOLD(4)                                 actor folds -> opponent wins the pot
FN_BOARD(5)   c0..c4 (5 bytes)             post the community board
FN_REVEAL(6)  hole0(1) hole1(1) nonce(32) idx0..idx4(5)   verify commit + rank the chosen five
FN_TIMEOUT(7)                              the overdue player's opponent claims the pot
```
Attach BIS only to `FN_STAKE0` / `FN_JOIN`. A revert commits nothing and transfers nothing.

## 3. The off-chain deal + web UI (`web/poker/`)

`relay.py` is a localhost signing/submit relay (like the AMM/DEX relays) **plus a tiny per-table message
bus** (`/relay/msg`, `/relay/inbox`) that the two browsers use to exchange deal messages — it only passes
messages along; the secret keys never leave the browsers. `index.html` is the table UI: it runs the
mental-poker deal, commits, plays/folds, reveals the board, and at showdown auto-picks the best five (a JS
clone of `_rank5`) and submits the reveal.

The deal (positions `0,1` = P0 hole, `2,3` = P1 hole, `4..8` = board):
1. P0 shuffles the 52-card deck and encrypts each card with key `k0`; sends it.
2. P1 shuffles that and encrypts with `k1` → a doubly-encrypted, doubly-shuffled deck; sends it back.
3. To deal P0's hole, P1 strips `k1` from positions `0,1` and sends the (still `k0`-encrypted) values to P0,
   who strips `k0` to read its cards — P1 learns nothing. Symmetrically for P1's hole. The board is revealed
   when both strip their keys from positions `4..8`.

## 4. Honest limits (demo-grade; mirror the other VM demos)

- **Off-chain deal trust:** card secrecy and shuffle fairness rest on the clients' mental-poker protocol; the
  chain verifies the hole-card commitments and that each player's five cards are distinct + in range, but it
  does **not** itself prove the shuffle was fair or that the board is the "real" decryption. A wrong board
  card is caught by the honest client (its own decryption differs) which then refuses to continue (the
  dispute is "don't proceed", then `FN_TIMEOUT`). Global 9-card uniqueness across both hands + board rests on
  the off-chain deal, not an on-chain check.
- **Demo crypto:** the SPA's SRA cipher uses a fixed prime and a plain card→value map; a production deal uses
  a large safe prime with quadratic-residue card encoding (to avoid Legendre-symbol leaks) and zero-knowledge
  shuffle proofs (or an on-chain verifiable shuffle / VRF).
- **All-in only:** no chip-by-chip street betting (raises, side pots) — the trustless core (escrow,
  commit-reveal, on-chain evaluation, settlement) is identical and is the documented extension point.
- **Heads-up only** (2 players); 32-bit unit amounts (`2*buyin <= 2^32-1`).

## 5. Tests

`python3 -m pytest tests/test_poker.py -v` — offline: the evaluator vs the Python reference across all
categories + ordering + tiebreaks; a full hand to showdown paying the better hand; tie-splits; fold;
commit-mismatch and duplicate-index guards; join/stake guards. Live regnet: deploy + P0 stake → custody holds
the buy-in and the table advances (the `SYS_TRANSFER` payout path is exercised live by the AMM/router/value
tests, same `vm_engine` custody). A full two-wallet live game is the documented follow-up.
