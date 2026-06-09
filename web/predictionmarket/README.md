# Bismuth Prediction Market — demo

A Polymarket-style **binary (YES/NO) prediction market** implemented as **one hand-authored RISC-V
contract** on the Bismuth decentralized-apps VM, with a clean single-page web UI.

- **Contract**: [`contracts/prediction_market.py`](../../contracts/prediction_market.py) — builds the
  RV32I bytecode. Users back YES or NO by depositing BIS into the contract's VM custody; a single
  baked-in **resolver** settles the outcome; winners **claim** a pro-rata share of the *whole* pot
  (winning + losing side). Guards: deposit must be > 0, resolver-only settle, no double-resolve, outcome
  ∈ {YES, NO}, claim-once, losers/non-stakers get nothing. Payout is parimutuel —
  `stake × (yes_pot + no_pot) / winning_pot` — computed with 64-bit `mulu64`/`divu64` software macros
  (RV32I has no hardware multiply/divide).
- **UI**: [`index.html`](index.html) — shows the question, YES/NO pools, implied odds %, pot, and
  resolution status; lets a user **buy** YES/NO, the resolver **settle**, and winners **claim**. Same
  dark Bootstrap theme as the block explorer, mobile-responsive.
- **State read**: the UI reads market state from the node's **read-only** REST API endpoint
  `GET /api/vm/market/{addr}` (added in `rest_api.py`; consensus-neutral — it only decodes the committed
  VM storage slots into named fields).
- **Signing/submit**: the node REST API is GET-only by design, so writes go through a small **local
  signing relay** ([`relay.py`](relay.py)) that signs with your wallet (the in-tree polysign, exactly
  like the test client) and submits over the node's socket protocol. The relay is **not** part of the
  node and changes nothing in consensus.

## How signing works (be explicit)

A browser can't sign a Bismuth RSA transaction or speak the node's length-prefixed socket protocol, and
the REST API intentionally exposes **no** write endpoint (so it can never affect consensus). So:

```
 browser SPA  ──GET /api/vm/market/{addr}──▶  node REST API (read-only state)
      │
      └──POST /relay/{buy,resolve,claim}──▶  relay.py  ──signs (wallet.der) + mpinsert──▶  node (socket)
```

The relay holds the wallet that signs. Run it locally next to a node you control; bind it to localhost
(it signs whatever the page asks). This mirrors how a real dApp would delegate signing to a wallet/signer
rather than embedding a private key in a web page.

## Run the demo (regnet)

All commands from the repo root. This uses **regnet** (a private local chain) — it does **not** touch
mainnet.

1. **Boot a regnet node** (REST API on `:3031`, socket protocol on `:3030`, VM enabled):

   ```bash
   cp tests/config_custom.txt config_custom.txt          # regnet + vm=True + rest_api on 3031
   python3 node.py regnet2
   ```

   Wait until `http://127.0.0.1:3031/api/status` responds.

2. **Mine past the VM fork.** The VM is gated behind the hf2 fork (here `fork_height=10`). The relay can
   mine on regnet:

   ```bash
   # in another terminal, from the repo root:
   python3 web/predictionmarket/relay.py --wallet wallet.der --node-port 3030 --listen 8099 \
       --api http://127.0.0.1:3031
   curl -X POST http://127.0.0.1:8099/relay/mine -H 'Content-Type: application/json' -d '{"count":14}'
   ```

3. **Serve the UI** (any static server; here Python's):

   ```bash
   cd web/predictionmarket && python3 -m http.server 8088 --bind 127.0.0.1
   ```

   Open <http://127.0.0.1:8088/index.html>.

4. **Drive the market in the UI:**
   - The config bar is pre-filled: API `http://127.0.0.1:3031`, Relay `http://127.0.0.1:8099`.
   - Click **“deploy a new market with this wallet as resolver”** (in the wallet line) — it deploys the
     contract, mines, finds its address, and loads it. (Or paste an existing market address + **Load**.)
   - **Buy YES / Buy NO** with an amount of BIS — the deposit funds the pot; odds update.
   - **Settle market** (resolver only) — pick YES or NO wins.
   - **Claim winnings** — winners redeem their pro-rata share to a recipient address; the pot drains.

   On regnet the UI auto-mines a couple of blocks after each action so it confirms instantly. On a real
   chain, miners confirm the transactions and you omit the mine step.

## Run it headless / from the API (no browser)

The same flow over HTTP, exactly what the UI does (selectors: buy_yes=0, buy_no=1, resolve=2, claim=3):

```bash
R=http://127.0.0.1:8099 ; A=http://127.0.0.1:3031
ADDR=$(curl -s -XPOST $R/relay/deploy -d '{}' >/dev/null; \
       curl -s -XPOST $R/relay/mine -d '{"count":2}' >/dev/null; \
       curl -s "$A/api/vm/contracts?compress=none" | python3 -c 'import sys,json;print(json.load(sys.stdin)["contracts"][0])')
curl -s -XPOST $R/relay/buy     -d "{\"address\":\"$ADDR\",\"side\":\"yes\",\"bis\":2}" ; curl -s -XPOST $R/relay/mine -d '{"count":2}'
curl -s -XPOST $R/relay/buy     -d "{\"address\":\"$ADDR\",\"side\":\"no\",\"bis\":1}"  ; curl -s -XPOST $R/relay/mine -d '{"count":2}'
curl -s     "$A/api/vm/market/$ADDR?compress=none"          # yes_pot/no_pot/pot/odds/resolved
curl -s -XPOST $R/relay/resolve -d "{\"address\":\"$ADDR\",\"outcome\":1}"             ; curl -s -XPOST $R/relay/mine -d '{"count":2}'
curl -s -XPOST $R/relay/claim   -d "{\"address\":\"$ADDR\",\"recipient\":\"<your 56-hex addr>\"}"
```

## Tests

```bash
python3 -m pytest tests/test_contracts_offchain.py -q        # exhaustive contract behaviour (no node)
python3 -m pytest tests/test_vm_prediction_market.py -q      # full lifecycle on a regnet node
```

## Notes / limits

- The market **question** is an off-chain label (the contract stores only pools/pot/outcome), kept in the
  browser per contract address. Pools, pot, odds and resolution are all **on-chain**.
- 32-bit VM: pools and per-user stakes are 32-bit unsigned units (1 BIS = 1e8 units), so a single market
  here is comfortable up to a few BIS of total pot — fine for a demo. A production market would want the
  64-bit-aware storage a higher-level toolchain (C/Rust → RV32I) would bring.
