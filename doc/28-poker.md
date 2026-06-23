# doc/28 — Bismuth Decentralized Multiplayer Hold'em (Design v2)

Supersedes the heads-up demo design (v1). Folds in **every finding** from the adversarial security
review of the multiplayer/tournament extension; each design decision a finding *changed* is marked
**[SEC-Cn]** / **[SEC-Hn]** / **[SEC-Mn]** inline.

Headline lesson: the heads-up `contracts/poker.py` is sound **because** 2 players + a fixed `2*BUYIN`
pot mask two sharp edges of the engine (`bismuth_riscv.py`) — 32-bit caller identity and a
non-reverting `SYS_TRANSFER`. **Both edges become exploitable the moment we go to N seats and N-way
pots.** This doc treats them as load-bearing, consensus-critical requirements, not footnotes.

---

## 1. Goals

A fully decentralized, real-money, graphical, multiplayer Texas Hold'em on Bismuth — no operator, no
custodian, no trusted dealer.

1. **Multiplayer** — 2–9 seats per table (heads-up is the N=2 special case).
2. **Real betting** — chip-by-chip street betting (preflop/flop/turn/river), bets/raises/calls/checks/
   folds, min-raise enforcement, all-in (incl. all-in-for-less), **proper multi-way side pots**.
3. **Graphics** — a browser SPA: SVG felt, animated chips/cards, an action bar (slider, quick-bets,
   all-in), a shot-clock, side-pot display, showdown overlay.
4. **Accounts + leaderboard** — persistent per-player identity and stats (hands, net BIS, ROI, ITM%,
   best finish, biggest pot), a lobby, ranked leaderboards.
5. **Tournaments** — Sit-and-Go (SNG) and multi-table (MTT): escrowed buy-ins → chip stacks,
   block-height blind schedule, deterministic seating/rebalancing, prize-ladder payouts.
6. **Fully decentralized** — money custody, refereeing, and results all on-chain (state-root-committed);
   only the private deal, the UI, the lobby/leaderboard *view*, and message relaying are off-chain — and
   none of those can move or lock funds.
7. **Browser wallet** — players connect a wallet in the browser (MetaMask/Kukai-style) to sign the
   `vm:call` actions; running a localhost relay must not be required (see §8.1).

## 2. The one architecture (the load-bearing split)

```
  Browser SPA + WALLET (per player) ──reads (TRUTH)──▶ Node REST /api/vm/contract/{addr} [state-root, RO]
       │ wallet holds keys; signs vm:call txs             ▲              /api/poker/*  [poker_stats, RO]
       │ off-chain mental-poker deal (P2P bus)            │ poll storage slots → reconstruct TABLE
       ▼                                                  │ POST /api/transaction (signed)
  Bus (per-table message relay, key-blind) ──────────────┘
```

| Concern | Where | Why |
|---|---|---|
| Money custody (buy-ins, pot, prize pool, payouts) | **On-chain VM custody** (`SYS_TRANSFER` from `VM_SINK`, doc/19) | Only the chain enforces "funds leave only to a player." Rollback-deterministic, state-root-committed. |
| Refereeing each hand (betting legality, side pots, hand rank, eliminations) | **On-chain contract** (RV32I) | Same role `poker.py` plays, extended 2→N + all-in→full streets. |
| Private dealing | **Off-chain mental poker** over the bus | The VM is a pure function with no secret state; keys can never be on-chain. |
| Stats / leaderboards / lobby | **Off-chain indexer** (`poker_stats.py`, copying `rest_stats.py`) | Derived aggregates: a pure reproducible fold over canonical on-chain data. |
| Tournament orchestration | **On-chain for chip/money moves; deterministic logic in ONE contract** | Custody + results trustless; no-cross-contract-call forces "many logical tables in one contract" (Balancer-Vault). |
| Key custody + signing | **Browser wallet** (injected provider or web wallet) — §8.1 | Player keys never touch a server; the app is just a dApp front-end. |

**Consensus rule (single hf2 fork):** the only consensus-affecting pieces are the contract bytecodes
(`poker_table.py`, `tournament.py`). They use the *existing* VM syscall set
(`SSTORE/SLOAD/CALLER/CALLVALUE/NUMBER/SHA256/TRANSFER/HALT`) — **no new syscall** — so they ship as
ordinary `vm:deploy` bytecode the single hf2 fork already enables (VM is post-hf2). Everything else
(wallet, bus, SPA, indexer, REST) is off-chain / read-only. **No second fork signal, ever.**

## 3. Component / file layout

On-chain (`contracts/`): `poker.py` (exists, heads-up — kept as audited base + reused primitives:
`build`, `_set_deadline`, `_require_no_value`, `_gather5`, `_rank5`, `addr_word_key`, `board_key`);
**`poker_table.py` (NEW)** the N-seat cash table; **`tournament.py` (NEW)** Balancer-Vault many-logical-
tables; `router.py`/`prediction_market.py` (pattern refs); `asmtools.py` (`mulu`,`divu`,**`mulu64`,
`divu64`**, branch relaxation `_addr_map(relaxed)`, `_uniq`).

> **Resolved divergence:** cash play uses standalone `poker_table.py`; tournaments use ONE `tournament.py`
> with logical tables (no cross-contract calls). They share the same assembled betting/side-pot engine.

Off-chain: `web/poker/relay.py` (optional localhost signer / dev fallback) → primary signing moves to the
**browser wallet** (§8.1); a **bus** service for the mental-poker traffic; `web/poker/index.html`
(rewrite: graphical SPA); **`poker_stats.py` (NEW)** indexer copying `rest_stats.py` discipline (scoped to
poker contract addresses; **never full-scans the prod ledger**), `rest_api.py` (+`/api/poker/*`). Docs:
this file + **`doc/32-tournaments.md` (NEW)** + **`doc/33-wallet.md` (NEW, §8.1)**. Tests:
`tests/test_poker_mp.py`, `test_tournament.py`, `test_poker_stats.py`.

## 4. The table contract (`poker_table.py`)

### 4.1 Identity — full 28-byte address, never the 32-bit id **[SEC-C1]**
`SYS_CALLER` returns `caller & WMASK` (`bismuth_riscv.py:175`); SSTORE masks to 32 bits (:171). v1's
`party_id_of = int(addr,16) & 0xFFFFFFFF` (`poker.py:116`) is only the low 32 bits. Fine for 2 players;
with ≤9 seats/table + arbitrary tournament entrants + a leaderboard keyed on the same id, the birthday
bound bites ~77k identities and an attacker can grind a wallet whose low-32 matches a target in ~2³² work
to hijack a seat or redirect a payout. **Design:** store all **7 words** (28 bytes) of each seat's address
(`TAG_ADDR|s,w`); `_which_seat` compares the **full 7-word `SYS_CALLER` word-by-word** (spill all 7 words
to scratch before any other syscall — a0/a1 clobbered). The 32-bit `party_id` survives only as an
off-chain display/index key, never authorization/payout.

### 4.2 Storage layout (32-bit words; every accumulator guarded)
Globals: `S_MAGIC`(0 "POKR"|ver), `S_NSEATS`(1), `S_PHASE`(2), `S_DEADLINE`(3 SYS_NUMBER),
`S_BUTTON`(4, STORED state only [SEC-H2]), `S_SB`(5)/`S_BB`(6), `S_STREET`(7), `S_TOACT`(8),
`S_TOCALL`(9), `S_MINRAISE`(10), `S_LASTAGGR`(11), `S_NUMACT`(12), `S_NUMALLIN`(13), `S_NPOTS`(14),
`S_HANDNO`(15), `S_RESULTCNT`(16), `S_ESCROW`(17 closing-invariant bound), `S_DONEFLAG`(18).
Per-seat: `TAG_ADDR`(0x11|s,w ×7 identity+payout), `TAG_STACK`(0x12|s), `TAG_STREETBET`(0x13|s),
`TAG_HANDBET`(0x14|s drives side-pot layering), `TAG_STATE`(0x15|s 0empty/1seated/2active/3folded/
4allin/5busted), `TAG_COMMIT`(0x16|s,w ×8), `TAG_COMMITTED`(0x17|s DEDICATED flag, never a hash word),
`TAG_HOLE`(0x18|s,j), `TAG_REVEALED`(0x19|s), `TAG_RANK`(0x1A|s), `TAG_DECKH`(0x1F|s,w ×8 anchor).
Board/pots/results: `TAG_BOARD`(0x1B|k DERIVED from committed deal [SEC-C4]), `TAG_POTAMT`(0x1D|p),
`TAG_POTELIG`(0x1E|p N-bit mask), `TAG_RESULT`(0x20|seq append-only; count=S_RESULTCNT).

### 4.3 ABI (`operation="vm:call"`, `openfield="<addr>:<calldata_hex>"`, first 4 bytes = selector)
`0 FN_SIT(seat,addr28)+BUYIN`, `1 FN_LEAVE(seat)`, `2 FN_DEAL`, `3 FN_COMMIT(32)`,
`4 FN_DECK_DIGEST(seat,H32)`, `5 FN_BOARD(street,cards)` [checked vs committed deal, SEC-C4],
`6 FN_CHECK`, `7 FN_CALL`, `8 FN_BET(amount BE)` (raise; min-raise; all-in-for-less ok),
`9 FN_FOLD`, `10 FN_REVEAL(h0,h1,nonce32,idx0..4)`, `11 FN_TIMEOUT` (permissionless; FORFEIT not refund).
**Value attaches ONLY to FN_SIT** (and FN_REGISTER in tournaments); every other selector runs
`_require_no_value` and reverts. A bet moves internal chips from `TAG_STACK` (guard `bet<=stack` with
unsigned `bltu` [SEC-M4]), not attached BIS.

### 4.4 Betting state machine + side pots
Turn advance clockwise `i=(i+1); if i>=N: i-=N` (no DIV), skip folded/all-in/empty, **guard empty table**
before the modulo. Street closes when the cursor returns to `S_LASTAGGR` with all active matched to
`S_TOCALL`, or ≤1 seat can act (fast-forward). Min-raise: raise ≥ `S_TOCALL+S_MINRAISE`; ladder
accumulator guarded against the **total-custody** bound [SEC-C3/M4]; all-in-for-less doesn't reopen
betting (`S_LASTAGGR`). **Side pots built INCREMENTALLY at each all-in**, never reconstructed at showdown:
`_rebuild_pots` distinct-level scan over `TAG_HANDBET` (O(N²)≤81 unsigned compares, no DIV/sort);
eligibility = contributed-to-layer AND not-folded; folded chips stay in lower pots. **Closing invariant
[SEC-C2/H3]:** `Σ TAG_POTAMT == Σ TAG_HANDBET == S_ESCROW` (guarded), revert on mismatch.

### 4.5 Showdown & checked payout **[SEC-C2, SEC-H3]**
`_rank5` runs once per `FN_REVEAL` → `TAG_RANK[s]` (`_gather5` checks 5 indices distinct/in-range/
distinct cards). Award per pot in pot order: winners = not-folded AND eligible(p) AND revealed AND max
rank; `share=divu(pot,len)`; **odd chip to first winner clockwise from button WITHIN THAT POT's winner
set** [SEC-H3]. **Every `SYS_TRANSFER` return checked** (`bne(ret,1,"revert")`) — engine sets reg[10]=0
and keeps executing on insufficient balance (`bismuth_riscv.py:192-197`); v1 `_pay_pot` never checks
(harmless heads-up, catastrophic N-way). `poker_table.py` does NOT reuse `_pay_pot` verbatim. Final
guarded `Σ payouts == Σ pots == S_ESCROW` asserted before HALT; never HALT with an unsuccessful transfer.

### 4.6 Timeouts — un-wedgeable AND un-grindable **[SEC-C4]**
v1's `FN_BOARD` is unanimous-or-void (`poker.py:430`): with N players any one refusing voids the hand and
refunds, so a loser dodges every loss. **Design:** (1) board DERIVED from the committed deal (FN_BOARD
validates vs the `TAG_DECKH`/card-map chain; a refusenik is force-timed-out and the board still resolves —
no void/refund); (2) force-timeout = FORFEIT whole hand-bet to pot, never refund; (3) single rolling
shot-clock per hand (grief always EV-negative); (4) every phase keeps a `SYS_NUMBER` deadline +
permissionless `FN_TIMEOUT`.

### 4.7 RV32I feasibility (MEASURE before mainnet — **[SEC-M3]**)
Per-action O(N≤9); `_rebuild_pots` ≤81 compares; `FN_REVEAL` one `_rank5`. Worst `settle_showdown`: ≤81
compares + ≤9 `divu` + ≤9 checked transfers — under 1M gas (`bismuth_riscv.py:88`; SHA256=60 :184).
**Mandatory before mainnet:** assemble both, assert `len(code)` headroom under `mem_size=1<<16=64KB` (:50);
push `SCRATCH` 24000→~30000; re-measure size+gas **after** branch relaxation (`asmtools:308`); run
worst-case settle on the real engine, `gas_used<1M`; keep `_rank5` one-per-call; deploy invariant
**`N*max_buyin ≤ 0x3FFFFFFF`** (side-pot carry headroom) [SEC-C3].

## 5. N-player mental-poker deal + anchoring + cheater detection
Generalize the 2-party SRA shuffle to N over the bus. Deck: positions `0..2N-1` holes (seat s → `2s,2s+1`),
`2N..2N+4` board. Protocol: (1) encrypt-shuffle round S_0→…→S_{N-1}; (2) **per-position re-keying
(Barnett–Smart locking)** so any single position opens independently; (3) deal holes — every other player
posts partial decryptions for only {2r,2r+1}, **seat r strips its own key LAST**; (4) board — all live
players partial-decrypt that street's positions. Anchoring chain: `TAG_CARDMAP_H` → `TAG_DECKH[i]` →
`TAG_COMMIT[s]` → board derived → FN_REVEAL (commit check + GLOBAL distinctness scan + `_rank5`).

**Chain enforces:** holes match commitments; 9 played cards globally distinct + in-range; better five wins;
folder/timed-out forfeits; board is the committed board. **Honest limits (NOT enforced) [SEC-H1]:** the
chain pins each deck *hash* but does not verify an honest shuffle/re-key; secrecy needs the enforced
locking round + ≥1 honest shuffler + victim-key-last; the client **refuses to reveal any hole openable
without its own key**. Soft collusion irreducible. Demo SRA must move to a **large safe prime + QR card
encoding** before real money. ZK-shuffle proofs = production hardening. **Metadata side channel [SEC-M1]:**
victim positions are **e2e-encrypted to the victim**; timing is a documented honest-limit.

## 6. Tournaments (`tournament.py`)
ONE contract, logical tables (`tid`), Balancer-Vault, embeds the §4 engine; **all §4 fixes apply per
table**. Betting selectors gain a leading `tid`; value only on `FN_REGISTER`. Escrow: `FN_REGISTER+buyin`
→ `S_PRIZE_POOL += buyin` (guarded), credits `starting_chips`; pool leaves only to finishers via checked
transfer. Formats: SNG (Nth register flips PH_REG→RUNNING); MTT (`FN_START` at `start_height` seats
round-robin; late-reg until `late_reg_height`). **Clock = block height only** (reorg-safe; schedule baked
at build). **Block height gates DEADLINES + BLIND LEVELS ONLY — never chip/seat/button/odd-chip [SEC-H2]**
(grindable + permissionless callers): button `(prev+1) mod active` from STORED state; seating
deterministic from entry order. **Prize math MUST use `mulu64`/`divu64` [SEC-C3]** (pool*pct overflows 32
bits before the divide; `prediction_market.py` pattern). **Idempotent reorg-safe transitions on every
permissionless selector [SEC-H4]** (read-verify-write a monotonic flag same-call; registration idempotent
per full identity; terminal flags set-once). Un-wedgeable: per-phase deadline + `FN_TIMEOUT(tid)` forfeit;
global stall pays remaining alive by chip count.

## 7. Accounts + leaderboard (indexer, not a contract)
No LOG opcode → canonical results are append-only storage records the indexer reads via
`/api/vm/contract/{addr}` (state-root-backed). `TAG_RESULT|seq`: R0 `hand_seq|street`, R1 winner ref
(`0xFFFFFFFF`=split), R2 `pot_units`, R3 `category<<20|tiebreak`; `S_RESULTCNT` high-water.
`poker_stats.py` copies `rest_stats.py`: CHEAP per-request (cache + `{"status":"computing"}` first call);
HEAVY `_maintain` daemon scanning **only poker contract addresses** (never full prod-ledger scan),
per-ledger JSON cache namespaced by ledger filename; account keyed by **full 28-byte address** [SEC-C1];
**reorg-safe + deduped by `(contract,block,seq,txid)`** [SEC-M2]. **Trustless = pure deterministic fold;
the leaderboard never gates money.** REST (RO): `/api/poker/account/{addr}`,
`/api/poker/leaderboard?metric=...&top=`, `/api/poker/tournaments`, `/api/poker/tournament/{addr}`.

## 8. Graphical client + wallet + bus
SPA never trusts a relay for truth — rebuilds `TABLE` every poll from `GET /api/vm/contract/{addr}`
(slot→value map) via read-key helpers mirroring `poker.py`. Card encoding byte-identical to the contract
(`rank=card%13`, `suit=card//13`). UI: SVG felt, ≤9 seats on an ellipse, `cardSVG`/`cardBack`/`chipStack`,
action bar (slider + min-raise + quick-pot + all-in), block-height shot-clock ring, side-pot display,
showdown overlay. Routes `#/lobby` (reads `/api/vm/contracts` filtered by `S_MAGIC`), `#/table/{addr}`,
`#/tourney/{addr}`, `#/profile/{addr}`.

### 8.1 Browser wallet integration (MetaMask/Kukai-style) — doc/33
Players sign actions with a **browser wallet**, never a server. Two interoperable forms:
- **Injected provider (MetaMask-style):** a `window.bismuth` provider (browser extension or a small
  injected script) exposing `connect()`, `getAddress()`, `signAndSend(tx)`, `signMessage(m)`. The SPA
  builds the unsigned `vm:call` tx (operation + `openfield="<addr>:<calldata_hex>"` + value for FN_SIT),
  hands it to the provider to sign, and the provider POSTs it to a node's `/api/transaction`. The
  Bismuth signing scheme is the existing `polysign` (ECDSA/RSA/ED25519); the wallet holds the key.
- **Web wallet (Kukai-style):** a hosted key-vault page (wallet.bismuth.*) the SPA opens via popup/
  redirect; the dApp sends the unsigned tx, the user approves in the wallet origin, the wallet signs +
  broadcasts and returns the txid. Keys stay in the wallet origin (encrypted, never exposed to the dApp).

The existing **localhost `relay.py` becomes the dev/self-host fallback** (a local "wallet" for testing /
power users), exposing the same `signAndSend` shape, so the SPA targets one signer interface regardless.
The **mental-poker bus** is separated from signing: it's a key-blind message relay (the existing
`/relay/msg`+`/inbox`, or any pubsub) carrying opaque deal traffic; it never sees keys and can't move
funds. Security: the wallet shows the human-readable action (sit/bet N/fold) decoded from the calldata
before signing; the dApp origin is displayed; value-bearing `FN_SIT`/`FN_REGISTER` require explicit
amount confirmation. doc/33 specifies the provider API + the decode-for-display mapping.

## 9. Security — must-haves before any real-money play
1. **[SEC-C1]** Full 28-byte identity for all auth + payout. 2. **[SEC-C2]** Check every `SYS_TRANSFER`
return + revert; on-chain `Σ payouts == Σ pots == S_ESCROW` before HALT; never HALT with an unsuccessful
transfer. 3. **[SEC-C3]** 64-bit `mulu64`/`divu64` for pot/prize products; guards to `N·max_buyin ≤
0x3FFFFFFF`. 4. **[SEC-C4]** Board derived from the committed deal; force-timeout = forfeit (never refund).
5. **[SEC-H2]** No chip/seat/button/odd-chip decision reads `SYS_NUMBER`. 6. **[SEC-H4]** Idempotent
reorg-safe transitions; registration idempotent per full identity; terminal flags set-once. 7. **[SEC-M3]**
Assemble + MEASURE worst-case gas + code size on the real engine; `SCRATCH`→~30000 after branch
relaxation. 8. **Production crypto** — implement-or-loudly-gate the §5 honest-limits before real money.
9. **Wallet:** the wallet must decode + display the action (and any value) before signing; the dApp never
sees the key; the bus is key-blind.

## 10. Staged, regnet-testable build plan
| Stage | Deliverable | Consensus? | Test gate |
|---|---|---|---|
| **1. Multiplayer betting + side-pots contract** | `contracts/poker_table.py` (N seats, 4-street SM, incremental side pots, N-way showdown reusing `_rank5`/`_gather5`, **[C1]** full-address id, **[C2]** checked transfers + closing invariant, **[C3]** 64-bit/custody guards, **[C4]** forfeit + deal-derived board, `TAG_RESULT`, `S_MAGIC`) | **YES** — bytecode on existing VM (single hf2 gate, no new syscall) | `tests/test_poker_mp.py`: offline side-pot/eligibility/min-raise/all-in-shortfall/multi-way-split/closing-invariant/checked-transfer-revert vectors vs a Python reference; live regnet 3-handed hand |
| **2. N-player off-chain deal** | `relay.py`/bus N-party broadcast + N-party SRA (safe prime + QR + per-position re-keying); `FN_DECK_DIGEST` anchoring; distinctness scan; victim-key-last + e2e holeparts | Off-chain (anchor slots in Stage-1 bytecode) | offline N-party deal sim (holes secret vs N−1 coalition); regnet end-to-end deal |
| **3. Graphical table + WALLET** | `web/poker/index.html` rewrite (SVG felt, ≤9 seats, action bar, shot-clock ring, side-pot display, showdown); **browser-wallet signer interface (§8.1) + relay fallback**; `#/table/{addr}` poll loop; `doc/33-wallet.md` | Off-chain | render regnet state; card encoding byte-identical; sign+send a real action via the wallet interface on regnet |
| **4. Accounts/leaderboard indexer** | `poker_stats.py` (copy `rest_stats.py`) + `/api/poker/*`; `#/profile`+lobby leaderboard; full-address key; `(contract,block,seq,txid)` dedupe **[M2]** | Off-chain, RO | `tests/test_poker_stats.py`: cold-build then incremental reproduce identical leaderboard; cache namespacing; `hdd_block` rollback; scoped scan |
| **5. Tournaments + lobby** | `contracts/tournament.py` (SNG→MTT escrow→pool, block-height blinds, **[H2]** stored-state seating, **[H4]** idempotent transitions, **[C3]** 64-bit payout, finish records); relay/wallet register/start/rebalance/payout; SPA `#/tourney`; `doc/32-tournaments.md` | Contract = YES (hf2 gate, no new syscall) | `tests/test_tournament.py`: escrow→pool, deterministic seating/rebalance replay, level advance, full SNG to payout, global-stall payout, reorg-idempotent register |

**Dependency chain:** Stage 1 prerequisite for all. Stages 2 & 3 parallel after Stage 1. Stage 4 needs
Stage 1's `TAG_RESULT`. Stage 5 needs Stage 1's engine + Stage 4's indexer for its lobby. The browser
wallet (§8.1) lands with Stage 3 and is reused by Stage 5.

## 11. hf2 / consensus gating
Consensus (all ride the single hf2 fork; VM post-fork; NO new syscall, NO second signal):
`poker_table.py` + `tournament.py` bytecode. Off-chain/non-consensus: the mental-poker deal, MTT
orchestration display, `poker_stats.py` + `/api/poker/*`, the SPA, the wallet, the bus.

## 12. Open questions
1. Production mental-poker crypto timeline (regnet demo-grade first; gate mainnet on safe-prime+QR +
ZK-shuffle?). 2. Shot-clock length vs UX. 3. Retire/alias `poker.py` to `poker_table.py` at N=2 or keep
as a minimal audited reference? 4. On-chain ZK-shuffle ever worth a new syscall? 5. Late-reg vs
`S_ESCROW`/blind-level in MTT [SEC-H4]. 6. Indexer dispute UX. 7. Wallet form: ship the injected-provider
spec + the localhost-relay fallback first, and add a hosted web wallet later? Which existing Bismuth
wallet (TornadoWallet) to extend into a dApp signer?

**Honest limits (documented, not fixed):** soft collusion irreducible; shuffle fairness rests on the
off-chain protocol + ≥1 honest shuffler; demo SRA must move to safe prime + QR; MTT seating
deterministic-not-random; the bus leaks routing metadata (mitigated by e2e-encrypting holeparts). The
[SEC-H1] secrecy claim is conditional on the enforced locking round + victim-key-last.
