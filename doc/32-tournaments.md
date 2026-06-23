# doc/32 — Bismuth Poker Tournaments (SNG + MTT) — as built (Stage 5)

This is the design **as implemented** for Stage 5 of doc/28 (§6, §10). It ships:

- `contracts/tournament.py` — ONE hand-authored RV32I contract, the Balancer-Vault for MANY logical poker
  tables (`tid`), each running the proven §4 betting/side-pot/showdown engine (the same one in
  `poker_table.py`), embedded here **namespaced per tid**. Custody of every table's chips and the single
  prize pool lives in this one contract (no cross-contract calls — the VM has none).
- `tests/tournament_ref.py` — the pure-Python lifecycle ORACLE (escrow → pool, blind schedule by height,
  deterministic seating, eliminations, finisher payout) that conserves Σbuyins == pool == Σpayouts.
- `tests/test_tournament.py` — VM vectors through `bismuth_riscv.execute`, validated against the oracle.
- `web/poker/lobby.html` + `web/lib/bismuth-tourney.js` — the lobby SPA + tournament calldata builders.

Consensus: the contract bytecode rides the **single hf2 fork** like `poker_table.py` (VM is post-hf2, no
new syscall, **no second fork signal**). Everything else (lobby SPA, indexer) is off-chain / read-only.

---

## 1. The funds model — conservation is the headline invariant

    Σ FN_REGISTER buy-ins  ==  S_PRIZE_POOL  ==  Σ payouts to finishers

- **Buy-ins (BIS)** attach **only** to `FN_REGISTER`. Each registration escrows the exact buy-in into the
  **single** `S_PRIZE_POOL` (a guarded 32-bit add) and credits the player **STARTING_CHIPS**.
- **Tournament chips** are a separate accounting unit, NOT BIS. They are credited into the §4 seat stack at
  seating and are conserved per table by the §4 **closing invariant** (`Σ pot == Σ hand_bet == TS_ESCROW`,
  asserted in `_eng_rebuild_pots` and again before each hand's settle). Chips **never leave custody**.
- **The prize pool leaves custody only to finishers**, on `FN_FINALIZE`, via **checked** `SYS_TRANSFER`
  (revert if the engine returns 0; never HALT on a failed transfer [SEC-C2]).
- The finisher **ladder** (e.g. SNG `50/30/20`) is computed with **`mulu64`/`divu64`** [SEC-C3]:
  `share_k = pool * permille_k // 1000`. `pool*permille` overflows 32 bits before the divide, hence the
  64-bit product. The **remainder** `pool − Σ shares` is added to **place 1 deterministically**, so no unit
  is minted or burned. Conservation `Σ payouts == pool` (and `pool == buyin·nreg`) is asserted **before any
  transfer**.

`tests/tournament_ref.payout_split` is the oracle for this split; the contract reproduces it bit-for-bit.

---

## 2. The clock [SEC-H2] — block height gates DEADLINES + BLIND LEVELS ONLY

`SYS_NUMBER` (block height) is read in exactly four places, all non-funds:

1. **Per-phase deadline** (`_set_deadline`) and the **`FN_TIMEOUT(tid)`** deadline check.
2. **Blind level lookup** (`_eng_blinds_for_height`): `level = (height − S_START_BLOCK) // level_blocks`,
   clamped to the last level; the `(sb,bb)` schedule is baked into the bytecode at `build()`.
3. The **MTT `start_height`** gate on `FN_START` and the **`late_reg_height`** gate on late registration.

**Nothing chip/seat/button/elimination/odd-chip/payout reads height:**
- the **button** rotates `(prev+1) → next occupied` from the **stored** `TS_BUTTON` [SEC-H2];
- **seating** is deterministic from **entry order** (see §4);
- **eliminations** are read from **stored chip stacks** (`FN_BUST`), bottom-up, deterministic;
- the **odd chip** goes to the first winner clockwise from the stored button within the pot's winner set.

`tests/test_tournament.test_no_sys_number_in_funds_decisions` asserts (by source inspection) that the
funds-critical functions contain no `a.number()`; `test_height_only_gates_blinds_not_seating_or_payout`
proves seating/button/identity are byte-identical across heights while only the blinds advance.

---

## 3. Idempotent / reorg-safe transitions [SEC-H4]

Every permissionless selector is read-verify-write on a monotonic / set-once flag in the same call:

- **`FN_REGISTER`** is idempotent **per full identity**: a repeat register by the same caller (low-32) is a
  no-op — no second escrow, no second entry (`_find_entrant_of_caller`).
- **`FN_START`** sets `S_RUNNING_STARTED` once; a second start reverts.
- **`FN_BUST(tid)`** clears an entrant's `alive` flag **set-once** (skips already-busted seats); replaying it
  is a no-op (`test_bust_and_finalize_idempotent`).
- **`FN_FINALIZE`** sets `S_TPHASE = TPH_DONE` and `S_NPLACES_PAID` (set-once) **before** transfers, and each
  entrant's `PAID` flag is claim-once; a second finalize reverts.

---

## 4. Formats, seating, embedding

### Embedding the §4 engine per tid
Every per-table storage key is `region(tid) + offset`, where `region(tid) = TID_BASE (0x40000000) +
tid·TID_STRIDE (0x01000000)`. The per-table **globals** (phase, button, street, to_act, to_call, min_raise,
escrow, winbyfold, npots, hand_no, sb, bb) and per-seat **tags** (addr, stack, street/hand bet, state,
commit, acted, rank, revealed, card, pot amount/elig, **entidx**) live inside that region with the SAME
layout `poker_table.py` uses, so each logical table runs the identical, already-audited betting logic. The
betting selectors (`FN_DEAL/COMMIT/CHECK/CALL/BET/FOLD/REVEAL/TIMEOUT/SETTLE`) each carry a **leading tid**
in `calldata[4..8]`; the dispatcher loads + validates `tid < S_NTABLES`, computes `region(tid)` into the
scratch `SC_TIDOFF`, and the engine adds it to every key. `TT_ENTIDX|seat` links a table seat back to its
entry index so `FN_BUST` can assign finish places.

### SNG (FMT_SNG)
One logical table (tid 0). The **Nth** registration (`target_entrants`) flips `PH_REG → RUNNING`, computes
`S_NTABLES = 1`, and seats every entrant on tid 0 with `seat == entry index`. Hands then run via the §4
selectors on tid 0; `FN_BUST(0)` eliminates 0-chip seats; `FN_FINALIZE` pays the ladder when one is alive.

### MTT (FMT_MTT)
`FN_START` (permissionless, once `height ≥ start_height`) flips RUNNING, computes
`S_NTABLES = ceil(nreg / nseats)`, and seats round-robin from entry order: entrant `k → tid k % ntables,
seat k // ntables` (deterministic, never height/random [SEC-H2]). **Late registration** is allowed while
RUNNING until `late_reg_height`; a late entrant takes its deterministic seat immediately.

> **MTT capacity note.** Round-robin `seat = k // ntables` must fit `nseats`; size `target_entrants`,
> `nseats` and the implied table count so seats stay in range (the contract bounds
> `nseats·starting_chips ≤ 0x3FFFFFFF` [SEC-C3] but does not auto-rebalance tables — table balancing across
> the field as players bust is a documented Stage-5 honest-limit, handled by re-seating policy off-chain;
> the on-chain custody + per-table settlement are complete).

---

## 5. ABI (operation `vm:call`, openfield `<addr>:<calldata_hex>`, first 4 bytes = selector)

| Sel | Name | Calldata | Value | Notes |
|----|------|----------|-------|-------|
| 0  | `FN_REGISTER(addr28)` | sel ‖ addr(28) | **buy-in** | escrow → pool, credit chips, seat; idempotent |
| 1  | `FN_START` | sel | 0 | MTT only; seat round-robin once `height ≥ start_height`; set-once |
| 2  | `FN_DEAL(tid)` | sel ‖ tid | 0 | start a hand on `tid`; blinds from the height level |
| 3  | `FN_COMMIT(tid, c32)` | sel ‖ tid ‖ commit(32) | 0 | anchor the showdown commitment |
| 6  | `FN_CHECK(tid)` | sel ‖ tid | 0 | |
| 7  | `FN_CALL(tid)` | sel ‖ tid | 0 | |
| 8  | `FN_BET(tid, target)` | sel ‖ tid ‖ target(BE32) | 0 | raise/all-in (chips, not BIS) |
| 9  | `FN_FOLD(tid)` | sel ‖ tid | 0 | |
| 10 | `FN_REVEAL(tid, c0..4, nonce32)` | sel ‖ tid ‖ 5 cards ‖ nonce(32) | 0 | ranked on-chain via `_rank5` |
| 11 | `FN_TIMEOUT(tid)` | sel ‖ tid | 0 | permissionless forfeit after the deadline |
| 12 | `FN_SETTLE(tid)` | sel ‖ tid | 0 | multi-way settle, credits winnings to stacks (chips stay in custody) |
| 13 | `FN_BUST(tid)` | sel ‖ tid | 0 | permissionless; eliminate 0-chip seats, assign finish places |
| 14 | `FN_FINALIZE` | sel | 0 | pay the finisher ladder from the pool; conservation asserted; PH_DONE |

**Value attaches ONLY to `FN_REGISTER`.** Every other selector runs `_require_no_value` and reverts on
attached BIS (the engine then refunds the sender), so attached value can never be stranded in custody.

---

## 6. Storage layout

Tournament globals (small slots): `S_TPHASE(1)`, `S_PRIZE_POOL(2)`, `S_NREG(3)`, `S_NTABLES(4)`,
`S_NALIVE(5)`, `S_NPLACES_PAID(6)`, `S_RUNNING_STARTED(7)`, `S_START_BLOCK(8)`.
Per-entrant (entry order `e`): `TAG_ENT_ADDR(0x30)|e,w` (7 words), `TAG_ENT_ALIVE(0x31)|e`,
`TAG_ENT_PLACE(0x32)|e`, `TAG_ENT_TID(0x33)|e`, `TAG_ENT_SEAT(0x34)|e`, `TAG_ENT_PAID(0x35)|e`.
Per-table (region `0x40000000 + tid·0x01000000`): globals `TS_*` + per-seat `TT_*` mirroring `poker_table`.

`tournament.read_entrant(storage, e)` decodes an entrant for the indexer / lobby (full 28-byte address +
alive/place/tid/seat/paid). The finisher record is the entrant's `(place, paid, addr)`; the per-hand
`TAG_RESULT` log lives in the embedded §4 engine exactly as `poker_table.py` documents it.

---

## 7. Gas / size [SEC-M3] (measured on the in-tree engine)

- `tournament.py` (9-seat SNG) assembles to **~22.8 KB** — well under the `mem_size = 64 KB` cap.
- Heaviest call measured: a **9-way showdown settle ≈ 2.1 k gas**; `FN_DEAL ≈ 1.8 k`, `FN_BET ≈ 1 k`,
  `FN_FINALIZE` (3-place ladder) is a short loop — all far under the **1,000,000-gas** budget.
- `SCRATCH` sits at 30000 (clear of code + calldata); `nseats·starting_chips ≤ 0x3FFFFFFF` is enforced at
  `build()` so every layered side-pot accumulator stays inside 32 bits [SEC-C3].

---

## 8. Honest limits (documented, not fixed)

- **MTT table balancing**: on-chain seating is round-robin at start + deterministic for late entrants; the
  field is **not** auto-rebalanced as tables thin out (off-chain re-seating policy; custody/settlement are
  fully on-chain).
- Seating is **deterministic, not random** (shared §4 / doc/28 honest-limit).
- The mental-poker deal secrecy rests on the off-chain protocol (doc/28 §5); the chain pins hashes, ranks
  revealed hands, and enforces every funds move.
