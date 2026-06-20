# 15 — Modern REST API (parallel, opt-in)

The node can expose a modern HTTP/JSON REST API **in addition to** the legacy length-prefixed-JSON
socket protocol ([06](06-networking-protocol.md)). The two run side by side: existing peers and
wallets keep using the socket protocol unchanged, while new clients can use the REST API.

The key difference is the concurrency model. The legacy protocol is a **serial pipeline** — a client
opens a socket and exchanges framed messages one after another on that single connection. The REST
API uses a `ThreadingHTTPServer`: **each HTTP request is handled concurrently in its own thread**,
with its own short-lived DB handler, so many clients can query in parallel without head-of-line
blocking.

Implementation: `rest_api.py` (`BismuthRESTServer`, a daemon thread). It is started from `node.py`
startup only when enabled.

## Enabling it

In `config.txt` (or `config_custom.txt`):

```
rest_api=True
rest_api_port=5659      # optional; default 5659
```

It is **disabled by default** — a node with no REST config behaves exactly as before, so old
deployments and old clients are unaffected. **Reads** (GET) never affect consensus. **Transaction
submission** (`POST /api/transaction`) is now implemented as the post-hardfork transport that moves
submission off the socket protocol onto the API — it is gated by a separate `rest_api_write=True`
flag (off by default) so a read-only node stays read-only until an operator opts in. A submitted tx
goes through the **same `mempool.merge` validation** the socket `mpinsert` uses (signature, balance,
duplicate, format), so the endpoint is a new transport, not a new consensus rule or a bypass.

## Endpoints (v1)

| Method & path | Returns |
|---|---|
| `GET /api` | API index (name, version, endpoint list) |
| `GET /api/status` | node status: protocol, version, blocks, last hash, difficulty, connections, consensus, uptime |
| `GET /api/difficulty` | the current difficulty tuple as a named object |
| `GET /api/block/height/{n}` | `{block_height, transactions[]}` |
| `GET /api/block/hash/{hash}` | `{block_hash, block_height, transactions[]}` |
| `GET /api/blocks/since/{h}?limit=N` | positive-height blocks after `h` (`limit` ≤ 1000) — for parallel sync |
| `GET /api/blocks/range/{start}/{end}` | blocks in `[start, end]` (span capped at 1000) — for parallel sync |
| `GET /api/balance/{address}` | `{address, balance}` (the O(1) balance index when enabled, else `ledger_balance3`) |
| `GET /api/transaction/{txid}` | a transaction (matched by signature prefix) |
| `GET /api/address/{address}/transactions?limit=N` | recent txs for an address (newest first; `limit` ≤ 500) |
| `GET /api/mempool` | `{count, transactions[]}` of pending txs |
| `GET /api/peers` | `{count, peers}` of known peers |
| `GET /api/headers/range/{start}/{end}` | block headers in `[start, end]` (lightweight headers-first sync) |
| `GET /api/nodes` | known nodes with reachable-API status + reputation (explorer node browser) |
| `GET /api/capabilities` | peer-sync capability descriptor: `version`, `node_version`, `rest_api`/`rest_port`, `compress` (transport codecs), `blocks`, `rest_api_write`, `testnet`/`regnet` — reachability of this endpoint is itself the REST-capable test |
| `GET /api/fork` | hf2 readiness: signalling %, lock-in state, activation height |
| `GET /api/fee` | current fee params: `base_fee` (demand-responsive post-fork), `vm_surcharge`, target/window |
| `GET /api/supply` | circulating BIS supply (background-computed; returns `"computing"` until the first scan finishes) |
| `GET /api/tokens` | issued tokens (from the token index) |
| `GET /api/token/{name}` | a token's supply + holders |
| `GET /api/token/tx/{address}?limit=N` | token transfers (sent or received) for an address, newest first; each a dict `{token, block_height, timestamp, sender, recipient, amount, signature}` (LMDB token index when enabled, else legacy index.db) |
| `GET /api/vm/contracts` | deployed contracts + the current VM `state_root`, `fork_height`, `enabled` |
| `GET /api/vm/contract/{addr}` | a contract: `engine` (`riscv`), code, custody `balance`, storage slots |
| `GET /api/vm/market/{addr}` | prediction-market contract state: pots, odds, resolution |
| `GET /api/shield/stats` | shielded pool (doc/22): note/nullifier counts, pool value, sink |
| `GET /api/shield/note/{note_id}` | public fields of a shielded note (nothing decryptable) |
| `GET /api/proxy?target={url}` | same-origin relay to another node's read-only `/api` (lets the https explorer browse an http node despite the browser's mixed-content block); read-only, GET-only, `/api`-paths-only, SSRF-guarded; gated by `rest_api_proxy` (default on) |
| `POST /api/transaction` | submit a signed tx (gated by `rest_api_write`; aliases: `POST /api/sendtx`, `POST /api/mempool`) |

Responses are JSON with appropriate status codes (`200`, `400` bad request, `403` forbidden, `404` not
found, `500` server error) and `Access-Control-Allow-Origin: *`. Transactions are formatted with
`essentials.format_raw_tx` (the same shape as the socket `*json` commands), so amounts/fields match
the legacy JSON responses.

Example:

```bash
curl http://127.0.0.1:5659/api/status
curl http://127.0.0.1:5659/api/block/height/1589600
curl http://127.0.0.1:5659/api/balance/4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed
```

## Browsing other nodes from the hosted explorer (mixed-content relay)

The explorer is served over **https** (explorer.bismuth.cz, nginx → node `127.0.0.1:5659`). When the
explorer's "connect node…" / "Nodes" browser points at a *peer* node — which speaks plain **http** on
`5659` — a browser refuses the cross-origin `fetch` (an https page may not load an http subresource:
*mixed content*). This is a browser policy, not a node problem; the peer's API is reachable (e.g. by
`curl`) the whole time.

`GET /api/proxy?target=<url>` solves it without weakening the browser rule. The explorer detects the
https→http case and routes the request through **its own** same-origin node (`/api/proxy`); that node
fetches the target's `/api` **server-side** (node→node http — no browser policy involved) and returns
the JSON. The browser only ever talks to its own https origin. The relay is locked down: GET only, the
target scheme must be http(s), the target path must be under `/api`, and the host must resolve to a
**public** address — loopback / private / link-local (incl. the `169.254.169.254` cloud-metadata IP) /
reserved targets are refused (`403`) so the node can't be used as an SSRF pivot. Disable entirely with
`rest_api_proxy=False`. Upstream status codes propagate (a target `404` returns `404`), and an
unreachable target returns `502`.

```bash
# relay another node's status through this node (what the explorer does under the hood)
curl "http://127.0.0.1:5659/api/proxy?target=http%3A%2F%2F185.100.232.5%3A5659%2Fapi%2Fstatus"
```

## Tested

`tests/test_rest_api.py` runs against a regnet node (`rest_api=True`, port 3031) and exercises the core
read endpoints + 404 handling. The newer endpoints are covered by their feature tests:
`/api/fee` by `test_fee_dynamics`/`test_transactions`; `/api/vm/*` by `test_vm_post_fork` + `test_vm_value`;
`/api/supply`, `/api/tokens`, `/api/nodes` by `test_explorer_endpoints`. The `/api/proxy` relay (happy
path + SSRF/scheme/path guards + the disabled state) is covered by the `proxy` tests in `test_rest_api`.

`POST /api/transaction` (the write path) is covered by `test_rest_api_write` — a signed tx submitted
over HTTP alone (no socket) moves funds; a malformed body is 400; an unsigned/garbage tx is accepted by
the endpoint but refused by mempool validation (never reaches a block).

## Roadmap

- **Writes** — ✅ `POST /api/transaction` (submit a signed tx) implemented, gated by `rest_api_write`,
  going through `mempool.merge`. Still to do: optional auth/rate-limit for public exposure, and migrating
  the demo relays + tooling off the socket `mpinsert` onto this endpoint (then the raw socket submit path
  can be retired).
- **Sync over REST** — ✅ read side added: `GET /api/blocks/since/{h}` and
  `GET /api/blocks/range/{start}/{end}` return positive-height blocks for **parallel** HTTP fetching
  (the serial socket sync stays for old peers). Still to do: a client-side parallel-fetch syncer and
  incremental rollup (doc/16 phase 6).
- **Pagination & filtering**, OpenAPI/Swagger description, optional API keys / rate limiting.
