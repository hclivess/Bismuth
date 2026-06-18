# doc/24 — DeFi primitives: on-chain exchange & atomic swaps

Post-fork building blocks for trustless trading on Bismuth, riding the in-core VM (doc/19) and the native
multisig signer (doc/23). The theme is **no operator, no custodian**: the chain itself escrows and settles.

| Primitive | Status | Where |
|-----------|--------|-------|
| §1 DEX — BIS⇄token limit order book | **implemented** | `contracts/dex.py`, `web/dex/`, `tests/test_dex.py` |
| §2 AMM — constant-product (x·y=k) pool | **implemented** | `contracts/amm.py`, `web/amm/`, `tests/test_amm.py` |
| §3 Router — multi-pool AMM, any token ⇄ any token | **implemented** | `contracts/router.py`, `web/router/`, `tests/test_router.py` |
| §4 HTLC — hash-time-locked contract (atomic swaps) | planned | composes §1 + doc/23 multisig |

---

## 1. DEX — a BIS ⇄ token limit order book (`contracts/dex.py`)

A decentralized exchange as one hand-authored RV32I contract. It issues one fixed-supply token
(genesis-minted to the deployer) and runs an order book trading that token against **native BIS**. There is
no operator: the contract escrows both legs and settlement moves BOTH or NEITHER (a failed `require` reverts
— no storage commit, no payout). This is the marketplace primitive HCLivess/damian flagged for trades, OTC
deals and seller/buyer protection, and the on-chain counterpart to an off-chain order book.

### Custody model (why it is trustless)
BIS lives in the contract's **VM custody** (a `vm:call` carrying value deposits into the VM_SINK, credited
to the contract before execution — EVM-style; doc/19). Token balances are an **internal ledger** in contract
storage. A trade either completes atomically or reverts:
- **SELL**: maker locks `token_amt` (debited from their token balance) and asks `price_bis`. A taker fills
  by attaching exactly `price_bis` → the taker is credited the token, the maker is paid the BIS by a forced
  `SYS_TRANSFER` (a consensus negative-height ledger row). No counterparty can take the token without paying.
- **BUY**: maker locks `price_bis` BIS (attached as value, held in custody) and bids for `token_amt`. A taker
  fills by spending token from their balance → the taker is paid the escrowed BIS, the maker credited token.
- **CANCEL** (maker-only): refunds the escrow — token back to the maker (SELL) or BIS back to the maker (BUY).

Because `SYS_TRANSFER` renders its 224-bit recipient as a 56-hex address and `CALLER` is the low 32 bits of
that same address int, a maker stored by full address is consistently identified by fold — so payouts reach
the real Bismuth address and the maker/taker accounting is sound (see `vm_engine`).

### Functions (selector = first 4 calldata bytes)
```
FN_INIT(0)                                  admin-only, once: mint the whole supply to the admin
FN_TRANSFER(1)  to(28) amt(4)               move token caller -> recipient
FN_SELL(2)      maker(28) token(4) price(4) lock token, list for BIS         -> returns order id
FN_BUY(3)       maker(28) token(4) price(4) lock price BIS (attach value), bid for token -> order id
FN_FILL(4)      oid(4) taker(28)            SELL: attach exactly price BIS;  BUY: spend token from balance
FN_CANCEL(5)    oid(4)                      maker-only: refund the escrow
```
`maker(28)` must be the caller's own address (the order is bound to its maker). Prices are atomic units (the
contract compares them to `callvalue`, which is in units); `web/dex/relay.py` takes prices in BIS and
converts.

### Storage
```
1 = minted flag           2 = next order id
0x10000000 | (party & 0x0FFFFFFF)        token balance (party = CALLER fold)
0x20000000 | (oid<<4) | field            order: 0 status(1 open,2 filled,3 cancelled) 1 side(1 SELL,2 BUY)
                                                 2 token_amt 3 price 4..10 maker address (28 bytes)
```

### Web UI (`web/dex/`)
A localhost signing relay (`relay.py`, like the raffle/prediction-market relays — the node API is read-only
and a browser can't sign) plus a single-page front-end (`index.html`) that reads the order book + balances
from `GET /api/vm/contract/<addr>` and POSTs deploy / init / transfer / sell / buy / fill / cancel / mine.
Demo only — bind the relay to localhost.

### Tests (`tests/test_dex.py`)
Offline (drive the bytecode through `bismuth_riscv.execute` with a custody-tracking harness): genesis mint
(admin-only, once), token transfer + overspend guard, SELL→FILL atomic settle, BUY→FILL atomic settle,
exact-payment + open-only fill, maker bound to caller, insufficient-balance sell, maker-only cancel with
escrow refund (token for SELL, BIS for BUY), and `build()` supply validation. Live regnet: deploy + genesis
mint + SELL→FILL (custody drains to the maker) + BUY→CANCEL (escrow refunded), all on the consensus path.

### Honest limits (demo-grade; mirror the other VM demos)
- **32-bit amounts**: token amounts, prices (BIS units) and balances are 32-bit, so a single price/amount
  tops out at ~2³² units (~42.9 BIS); order ids < 2²⁴. A production DEX wants 64-bit-aware storage.
- **Whole-order fills only** — no partial fills and no price-time matching engine; it is a fixed-amount
  limit book (the clean, overflow-free design for this VM). Matching/partial fills are a higher-level
  toolchain concern.
- **Single token per deployment** — one contract = one token vs BIS. Many tokens = many deployments.
- **No automated market maker** in the order book itself — that is a separate contract, now **shipped** as
  the constant-product AMM in **§2** (`contracts/amm.py`): slippage-priced swaps + LP shares, built on the
  same 64-bit `mulu64`/`divu64` macros the prediction market uses for exact pro-rata math.

---

## 2. AMM — a constant-product (x·y=k) BIS ⇄ token pool (`contracts/amm.py`)

The documented follow-up to §1, and the answer to "we need a market maker that's always open, like the big
exchanges run." An order book only trades when a maker and a taker meet; an AMM holds a standing reserve of
**both** assets and is *itself* the counterparty, so a swap always executes — at a price that moves against
size (slippage) off the invariant `R_bis · R_tok = k`. One hand-authored RV32I contract, same trustless
custody model as the DEX: the contract escrows both reserves and settlement is atomic.

### How "everyone wins (for a while)" actually works — honestly
Every swap pays a **0.3% fee** that stays in the pool. The fee makes the invariant `k` *grow*, so each LP
share is redeemable for steadily more of the reserves: **liquidity providers are paid by the trade flow they
enable** — the on-chain version of the spread a Binance-style market maker earns. Traders win on convenience:
instant fills, no waiting for a counterparty. That is the real, sustainable mechanism — *not* a promise that
prices only go up. **The honest caveat:** an LP carries *impermanent loss* — if the price moves far, the
pool rebalances into the falling asset and the LP can end up worse off than just holding. The fee compensates
for providing liquidity; it is not free money, and nobody wins forever. (`tests/test_amm.py` includes a
round-trip test proving the fee accrues to the LP, and the math below shows a swap can never drain a reserve.)

### Custody model (identical trust story to the DEX)
BIS lives in the contract's **VM custody**; the BIS reserve slot `R_bis` mirrors that custody exactly at all
times (every BIS in the contract belongs to the pool). Token reserves are an **internal ledger** in storage.
A failed `require` reverts — no storage commit, no payout — and the engine refunds any BIS attached to a
reverting call. Operations:
- **ADD_LIQUIDITY** — attach `dBis`; the contract pulls the *proportional* token from your balance and mints
  LP shares. The **first** add seeds the price (`R_bis=dBis`, `R_tok=tok_max`) and permanently locks a
  `MINIMUM_LIQUIDITY` of shares (Uniswap-style) so the pool can never be fully drained and re-seeded.
- **REMOVE_LIQUIDITY** — burn shares for the proportional BIS (forced `SYS_TRANSFER`) + token back.
- **SWAP BIS→token / token→BIS** — `out = floor(in_eff · R_out / (R_in + in_eff))`, `in_eff = floor(in·997/1000)`,
  with a caller-supplied `min_out` slippage floor. Because `in_eff/(R_in+in_eff) < 1`, `out < R_out` always —
  a swap can never empty a reserve.

### Functions (selector = first 4 calldata bytes)
```
FN_INIT(0)                                     admin-only, once: mint the whole supply to the admin
FN_TRANSFER(1)   to(28) amt(4)                 move token caller -> recipient
FN_ADD_LIQ(2)    tok_max(4)            +value  attach dBis; pull <= tok_max token; mint LP shares
FN_REMOVE_LIQ(3) recipient(28) shares(4)       burn shares -> proportional BIS (to recipient) + token
FN_SWAP_B2T(4)   min_tok_out(4)        +value  swap attached BIS -> token (>= min_tok_out, else revert)
FN_SWAP_T2B(5)   recipient(28) tok_in(4) min_bis_out(4)   swap token -> BIS (to recipient, >= min_bis_out)
```
`recipient(28)` must fold to the caller (payouts reach your real address). Attach value ONLY to ADD_LIQ /
SWAP_B2T. `web/amm/relay.py` takes BIS amounts in BIS and converts; token/share amounts are integer units.

### Storage
```
1 = minted flag    2 = R_bis (units)    3 = R_tok (units)    4 = L (total LP shares)
0x10000000 | (party & 0x0FFFFFFF)        token balance (party = CALLER fold)
0x20000000 | (party & 0x0FFFFFFF)        LP-share balance
```

### The 32-bit math — why it is provably overflow-free (`MAX_RESERVE = 2³⁰`)
The constant-product formulas multiply two ~reserve-sized values, which overflows a 32-bit word, so the
products are carried in a full 64-bit (hi:lo) pair via asmtools' `mulu64`/`divu64` — the same macros the
prediction-market payout uses. Each reserve is capped at **`MAX_RESERVE = 2³⁰` units (~10.7 BIS)**; any op
that would grow a reserve past it reverts (and a single ADD may at most match the current reserve). Crucially
each swap/add also bounds the **raw input** (`callvalue`/`tok_in`) to ≤ MAX *before* the 32-bit `R + in` add,
so that sum (≤ 2·2³⁰ < 2³²) can never wrap — a wrapped sum would otherwise slip under the cap guard and
desync custody (the audit below caught exactly this). With both
reserves ≤ 2³⁰: every 32×32 product ≤ 2⁶⁰ (fits 64 bits), every divisor (`R_in + in_eff`) < 2³² (fits the
32-bit `divu64` divisor word), and every quotient < 2³² (fits the result word — `divu64` requires this).
Register discipline note: the ECALL helpers use `a0/a1` as syscall argument registers (so `sstore`/`transfer`
clobber them), so live values ride only syscall-and-macro-safe registers (`t1,t2,t6,s0..s3`) or are reloaded
from calldata/`callvalue` — see the header of `contracts/amm.py`.

### Security audit (multi-lens, every finding independently verified)
An adversarial audit (overflow / register-clobbering / value-conservation / economic / ABI lenses) ran the
assembled bytecode and confirmed + fixed:
- **Critical — `swap_b2t` wrap-the-guard.** The cap was checked on the *wrapped* 32-bit sum `R_bis + in`, so
  a deposit of `~2³² − R_bis` (~42 BIS, not even truncated) wrapped the sum under MAX, drained ~all of the
  token reserve, and shattered `custody == R_bis`. Fixed by bounding the raw `in ≤ MAX` before the add (same
  guard added to `swap_t2b` so its safety no longer rests on `total_supply < 2³²`).
- **`callvalue` truncation (engine).** `vm_engine` credited custody with the full deposit while the VM
  exposes `callvalue & 0xFFFFFFFF`; a deposit ≥ 2³² stranded BIS for *any* value-bearing contract (DEX too).
  Fixed at the boundary: `vm_engine._call` now refunds, without executing, any deposit that does not fit a
  32-bit word — so the VM-visible `callvalue` always equals the BIS credited to custody.
- **Recipient binding** tightened to the full low-32-bit caller (matching the DEX) instead of low-28.
Regression tests for all three live in `tests/test_amm.py` (`test_swap_b2t_wrap_attack_reverts`,
`test_engine_refunds_oversized_deposit`, `test_recipient_binding_pins_full_low_word`), alongside a 400-op
property test asserting `custody == R_bis` and token conservation after every operation.

### Web UI (`web/amm/`) & tests (`tests/test_amm.py`)
A localhost signing relay (`relay.py`, like the DEX's) plus a single-page front-end (`index.html`) that reads
reserves / LP shares / balances from `GET /api/vm/contract/<addr>`, shows live BigInt swap/LP quotes (exact
to the contract's integer math), and POSTs deploy / init / transfer / add / remove / buy / sell / mine.
Tests: offline adversarial coverage (a Python reference recomputes every formula unit-for-unit; covers mint,
the MIN_LIQ lock, both swap directions + k-monotonicity, slippage floors, proportional add + its guards,
proportional remove + recipient binding, LP fee accrual, the MAX_RESERVE cap, and no-drain) plus a live regnet
test exercising the consensus custody+payout path (deposit credits custody, swap-out forces a BIS payout).

### Honest limits (demo-grade; mirror the other VM demos)
- **32-bit reserves, capped at ~10.7 BIS/side.** A production AMM wants 64-bit-aware storage (a real
  C/Rust → RV32I toolchain) to lift the cap; this is the clean, provably in-range design for the base VM.
- **One pool per deployment** — one contract = one token vs BIS. Many pairs = many deployments. No routing
  across pools, no flash-swaps, no concentrated-liquidity ranges.
- **Sub-unit rounding always favors the pool / existing LPs** (token pulled on ADD rounds up, shares and
  payouts round down), so rounding can never be farmed — but it does mean dust accrues to the locked shares.
- **Impermanent loss is real** (see the honest note above): the fee is the LP's compensation, not a guarantee.

---

## 3. Router — a multi-pool AMM: any token ⇄ any token (`contracts/router.py`)

The §2 AMM is one pool (one token vs BIS). "We need any token to swap to any other" means **routing**: a
token-A→token-B trade hops A→BIS→B. But this VM has **no cross-contract calls** (`vm_engine` runs exactly
one contract per `vm:call`), so a router cannot atomically call pool-A then pool-B if they are separate
deployments. The fix is the **Balancer-V2 "Vault" model**: one contract holds *every* pool's reserves, so a
token→token route is just two hops in a single execution against that contract's own storage — atomic by
construction, no cross-contract calls.

### What it is
One contract hosting up to `MAX_TOKENS` (16) BIS-paired constant-product pools. The admin `CREATE`s tokens
(each a fixed supply, minted to the admin, with its own pool `tid`). Per pool you can add/remove liquidity
and swap BIS⇄token exactly like the §2 AMM. The new primitive is **`SWAP_T2T(tid_in, tid_out, amt_in)`**:
- **hop 1** sells `amt_in` of token A into pool A for `bis_mid` BIS (0.3% fee, kept by pool A's LPs);
- **hop 2** buys token B out of pool B with that `bis_mid` (0.3% fee, kept by pool B's LPs);
both in one call. The intermediate BIS **never leaves custody** — it moves from `R_bis[A]` to `R_bis[B]` —
so no `SYS_TRANSFER` is needed and the contract-wide invariant **`custody == Σ R_bis[tid]`** is preserved.
A token→token trade therefore pays the fee twice (both pools' LPs earn) and either both hops settle or the
whole call reverts.

### Functions (selector = first 4 calldata bytes)
```
FN_CREATE(0)     supply(4)                          admin-only: mint a new token tid -> return tid
FN_TRANSFER(1)   tid(4) to(28) amt(4)               move token tid caller -> recipient
FN_ADD_LIQ(2)    tid(4) tok_max(4)         +value   add liquidity to pool tid (first add seeds + locks MIN_LIQ)
FN_REMOVE_LIQ(3) tid(4) recipient(28) shares(4)     burn shares -> proportional BIS (to recip) + token
FN_SWAP_B2T(4)   tid(4) min_tok_out(4)     +value   BIS -> token tid
FN_SWAP_T2B(5)   tid(4) recipient(28) tok_in(4) min_bis_out(4)   token tid -> BIS (to recip)
FN_SWAP_T2T(6)   tid_in(4) tid_out(4) amt_in(4) min_out(4)       token A -> BIS -> token B (atomic route)
```
`recipient(28)` low word must equal the full caller. Attach value ONLY to ADD_LIQ / SWAP_B2T.

### Storage (32-bit keys)
```
0 = next_tid                                     (tokens created; tid in 0..15)
0x10000000 | (tid<<4) | field    pool[tid]: 0 R_bis, 1 R_tok, 2 L
0x20000000 | (tid<<24) | (party & 0x00FFFFFF)    token balance (party = caller fold)
0x30000000 | (tid<<24) | (party & 0x00FFFFFF)    LP-share balance
```
The 4-bit `tid` and 24-bit party fold partition the 32-bit key space; `_require_tid` enforces
`tid < next_tid ≤ 16` on every op so a `tid<<24`/`tid<<4` can never spill into a neighbouring key domain.

### Overflow safety (same proof as §2, extended to the route)
Each reserve is capped at `MAX_RESERVE = 2³⁰`, and each swap/add bounds the **raw input ≤ MAX before any
add** (so `R + in` cannot wrap mod 2³², the AMM's prior critical bug). The route is two such swaps, and its
intermediate `bis_mid ≤ R_bis[A] ≤ MAX`, so every `mulu64`/`divu64` intermediate stays ≤ 2⁶⁰ and every
divisor/quotient < 2³² — identical bounds to the single pool. `R_bis[B] += bis_mid` is guarded ≤ MAX, so a
route too large for pool B's headroom reverts.

### Assembler note (branch relaxation)
At 6.4 KB the router is the first contract to exceed an RV32I conditional branch's ±4 KB reach, so
`asmtools.assemble()` now **relaxes** any out-of-range `b<cond>` into `b<inverted> +8; jal target` (jal
reaches ±1 MB), iterating to a fixpoint. In-range contracts (AMM/DEX) relax nothing and are byte-identical.

### Web UI (`web/router/`) & tests (`tests/test_router.py`)
A localhost signing relay + SPA that lists every pool, creates tokens, adds/removes liquidity, swaps, and
**routes** token→token with a live two-hop BigInt quote. Offline tests (Python reference recomputes every
formula incl. the two-hop route) cover create/transfer/add/remove, both single-hop swaps, the route
(matches the reference, both pools' k grow, custody unchanged), tid validation, the raw-input/recipient
guards, and a 500-op multi-pool property test asserting `custody == Σ R_bis` and per-token conservation
after every op. A live regnet test runs two pools + an atomic token→token route on the consensus path.

### Honest limits (demo-grade)
- **≤16 tokens, each reserve ≤ ~10.7 BIS** (32-bit keys + the 2³⁰ cap); **24-bit address fold** (vs 28 in
  the single-pool AMM) to make room for the tid, so addresses sharing their low 24 bits share a balance.
- **BIS is the only routing hub** — token→token always goes A→BIS→B (one hop each side); there is no
  multi-hop path-finding across >2 pools and no token↔token pools.
- A 64-bit-key store (a real C/Rust→RV32I toolchain) lifts the token count, reserve cap, and fold width.
- Same impermanent-loss caveat as §2: the fee is the LPs' compensation, not free money.

---

## 4. HTLC — hash-time-locked contracts for atomic swaps (planned)

damian's ask: a trustless, future-proof way to swap BIS for BTC/LTC/DOGE without a bridge or a trusted
custodian. The standard primitive (BIP-199) is an HTLC with two spend paths:

```
before timeout:  the recipient claims by revealing a secret S where HASH(S) == H
after  timeout:  the original funder refunds
```

This is directly buildable on the in-core VM, reusing the pieces already shipped:
- **hashlock** — the VM has `SYS_SHA256`; the claim path checks `SHA256(S) == H` for a committed `H`
  (exactly the raffle's commit-reveal, doc/19, but gating a payout instead of a draw).
- **timelock** — the VM has `SYS_NUMBER` (block height); the refund path checks `height >= deadline`.
- **payout** — `SYS_TRANSFER` forces the BIS leg to the claimant/refunder, no custodian.

A cross-chain swap is then two HTLCs sharing one secret `H` (one on Bismuth, one on the counter-chain): the
party who reveals `S` to claim one side necessarily exposes `S` on-chain, letting the counterparty claim the
other — atomic by construction, or both refund after timeout. The native multisig signer (doc/23 §2.1)
composes here for 2-of-2 cooperative-close / dispute paths.

Deferred vs. shipped deliberately: the DEX (§1) is a *same-chain* trustless exchange available now; the HTLC
adds *cross-chain* atomic swaps and is the next DeFi contract on this track.
