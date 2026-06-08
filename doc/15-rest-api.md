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
deployments and old clients are unaffected. v1 is **read-only** (GET only): it cannot affect
consensus. Transaction submission is intentionally deferred to a later, authenticated version (see
[14](14-known-issues-and-improvements.md)).

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
| `GET /api/capabilities` | which optional subsystems this node exposes (`vm`, `balance_index`, `block_store`, …) |
| `GET /api/fork` | hf2 readiness: signalling %, lock-in state, activation height |
| `GET /api/fee` | current fee params: `base_fee` (demand-responsive post-fork), `vm_surcharge`, target/window |
| `GET /api/supply` | circulating BIS supply (background-computed; returns `"computing"` until the first scan finishes) |
| `GET /api/tokens` | issued tokens (from the token index) |
| `GET /api/token/{name}` | a token's supply + holders |
| `GET /api/vm/contracts` | deployed contracts + the current VM `state_root`, `fork_height`, `enabled` |
| `GET /api/vm/contract/{addr}` | a contract: `engine` (`riscv`), code, custody `balance`, storage slots |

Responses are JSON with appropriate status codes (`200`, `400` bad request, `404` not found, `500`
server error) and `Access-Control-Allow-Origin: *`. Transactions are formatted with
`essentials.format_raw_tx` (the same shape as the socket `*json` commands), so amounts/fields match
the legacy JSON responses.

Example:

```bash
curl http://127.0.0.1:5659/api/status
curl http://127.0.0.1:5659/api/block/height/1589600
curl http://127.0.0.1:5659/api/balance/4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed
```

## Tested

`tests/test_rest_api.py` runs against a regnet node (`rest_api=True`, port 3031) and exercises the core
read endpoints + 404 handling. The newer endpoints are covered by their feature tests:
`/api/fee` by `test_fee_dynamics`/`test_transactions`; `/api/vm/*` by `test_vm_post_fork` + `test_vm_value`;
`/api/supply`, `/api/tokens`, `/api/nodes` by `test_explorer_endpoints`.

## Roadmap

- **Writes** — authenticated `POST /api/transaction` (submit a signed tx), mapping to the mempool.
- **Sync over REST** — ✅ read side added: `GET /api/blocks/since/{h}` and
  `GET /api/blocks/range/{start}/{end}` return positive-height blocks for **parallel** HTTP fetching
  (the serial socket sync stays for old peers). Still to do: a client-side parallel-fetch syncer and
  incremental rollup (doc/16 phase 6).
- **Pagination & filtering**, OpenAPI/Swagger description, optional API keys / rate limiting.
