# doc/24 — DeFi primitives: on-chain exchange & atomic swaps

Post-fork building blocks for trustless trading on Bismuth, riding the in-core VM (doc/19) and the native
multisig signer (doc/23). The theme is **no operator, no custodian**: the chain itself escrows and settles.

| Primitive | Status | Where |
|-----------|--------|-------|
| §1 DEX — BIS⇄token limit order book | **implemented** | `contracts/dex.py`, `web/dex/`, `tests/test_dex.py` |
| §2 HTLC — hash-time-locked contract (atomic swaps) | planned | composes §1 + doc/23 multisig |

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
- **No automated market maker** yet. A constant-product (x·y=k) AMM is feasible on this VM using the 64-bit
  `mulu64`/`divu64` macros (asmtools) that the prediction market already uses for exact pro-rata math; it is
  the natural next exchange contract (slippage-priced swaps + LP shares) and is the documented follow-up.

---

## 2. HTLC — hash-time-locked contracts for atomic swaps (planned)

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
