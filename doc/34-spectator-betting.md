# doc/34 — Spectator parimutuel betting on poker outcomes (design)

"Let people bet on which player wins." Non-seated spectators stake BIS on a seated player; when the hand
(or tournament) resolves, the stakes are paid out **parimutuel** (pool split pro-rata among everyone who
backed a winner) — settled **trustlessly from the canonical on-chain result, never a server**.

This is doc/28 Stage 6. Design only.

## 1. The load-bearing constraint: no cross-contract calls
The RV32I VM has **no cross-contract calls and no cross-contract storage reads** (the same constraint that
made `router.py` a single Balancer-Vault contract rather than calling AMM pools). Therefore a *separate*
`betting_market.py` could **not** trustlessly learn who won at a `poker_table` — it would need an oracle or a
trusted reporter, which violates "never a server."

**Decision:** spectator betting is an **ADDITIVE side-pool inside the contract that already determines the
result** — `poker_table.py` for "who wins this hand", `tournament.py` for "who wins the tournament". The same
`_settle` that computes the winner and holds escrow also holds and pays the side-pool. No oracle, no cross-
contract read, no server: settlement is a pure function of state the contract already owns. The side-pool is
kept **strictly separate** from the player pot (`S_ESCROW`) — the table's closing invariant is unchanged, and
the side-pool has its **own** conservation invariant.

## 2. Per-hand spectator market (additive to `poker_table.py`)
New storage (disjoint tag domains; player betting/pot/settle untouched):
* `S_SBET_POOL` — total spectator stake escrowed this hand.
* `TAG_SBET_SEAT|seat` — total staked on each seat this hand.
* `TAG_SBET|(bettorslot)` and `TAG_SBET_ADDR|(bettorslot,w×7)` + `TAG_SBET_PICK|bettorslot` — per-bettor
  record: full 28-byte address [SEC-C1], chosen seat, amount. `S_SBET_CNT` high-water (append-only, like
  `TAG_RESULT`), so settlement folds a fixed list.

New ABI (additive selectors; **value attaches** like `FN_SIT`):
* `FN_SPECTATE_BET(seat)` **+ value** — only while `PH_BET` and before a configurable `S_SBET_CLOSE`
  deadline (so no betting once information leaks toward showdown); bettor may be any address, **including a
  non-seated one**; a seated player may not bet on its own hand. Escrows value into the side-pool, appends a
  bettor record, bumps `TAG_SBET_SEAT|seat` and `S_SBET_POOL` (every accumulator wrap-guarded [SEC-C3];
  `N*maxbet` bounded so the pool stays in 32 bits, like the side-pot bound).
* `FN_SETTLE_SBETS` (permissionless) — callable once the hand is `PH_SHOWDOWN/SETTLED` and the winner(s) are
  known from `TAG_RESULT`/`S_WINBYFOLD`. Idempotent (a `S_SBET_DONE` flag; reorg-safe [SEC-H4]).

Settlement (parimutuel, `mulu64`/`divu64` [SEC-C3]):
* `winstake = Σ TAG_SBET_SEAT|w` over winning seats (split pot ⇒ multiple winning seats).
* Each bettor on a winning seat gets `payout = stake * S_SBET_POOL / winstake` (64-bit product before the
  divide). Odd-unit remainder assigned deterministically (first winning bettor clockwise from the button, as
  the side-pot odd chip is) so **Σ payouts == S_SBET_POOL** exactly — the side-pool conservation invariant,
  asserted before HALT; every transfer **checked** [SEC-C2].
* **No-winner-backed edge:** if `winstake == 0` (nobody backed a winner, or no bets), **refund every bettor
  their own stake** pro-rata (here exactly their stake) — never lock or mint. (Optional `rake_bps` to the
  table creator could be added later; default 0 = pure parimutuel.)

Determinism / fairness: the predicted seat and all chip/seat decisions come from stored state; **block height
only gates the betting-close deadline** — never the outcome [SEC-H2]. The result is the table's own `_rank5`
showdown, which spectators cannot influence (they hold no cards/keys).

## 3. Tournament-winner market (additive to `tournament.py`)
Same shape keyed by tournament: `FN_TBET(player)` + value during the registration / early phase; settled at
the finisher payout from the canonical 1st-place finisher. Parimutuel split among backers of the winner;
refund if the backed field is empty; conservation `Σ tbet payouts == tbet pool`.

## 4. Client
A spectator panel (a new `web/poker/betting.html`, not editing the table SPA) using `window.bismuth`:
connect wallet → see the seats and the **live parimutuel odds** (each seat's share of the pool ⇒ implied
payout multiple, read from `GET /api/vm/contract/<addr>` `TAG_SBET_SEAT`) → `FN_SPECTATE_BET(seat)+value` →
watch settlement. Reuses `bismuth-tx.js` calldata builders (extended with the new selectors).

## 5. Security checklist (mirrors doc/28 §9)
* [SEC-C1] bettor keyed by full 28-byte address. [SEC-C2] checked transfers + `Σ payouts == pool` before HALT.
* [SEC-C3] `mulu64`/`divu64`; every pool/stake accumulator wrap-guarded; `N*maxbet` bounded to 32 bits.
* [SEC-H2] block height gates only the betting-close deadline, never the outcome/payout.
* [SEC-H4] `FN_SETTLE_SBETS` idempotent + reorg-safe; side-pool strictly separate from `S_ESCROW` so the
  player closing invariant is byte-unchanged (the existing 68 tests must stay green).
* Indexer: spectator P&L can fold from the same append-only side-bet records into `poker_stats.py`
  (a later, optional add).
