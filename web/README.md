# web/ — deployed front-ends

Static front-ends served by this server's nginx, version-controlled here (no build step — vanilla JS +
vendored Bootstrap).

- **`explorer/index.html`** — the Bismuth block explorer, deployed at **https://explorer.bismuth.cz**.
  A single page over the node's read-only REST API (`/api/`, proxied same-origin by nginx):
  - live status + **circulating supply**, block paging, block / transaction / address / balance lookup;
  - **Tokens** (`/api/tokens`, `/api/token/{name}`) — per-token supply, holders, balances;
  - **Nodes** (`/api/nodes`) — browse the peer network: each node's height, version, **reputation**, and
    connected / banned / whitelisted status. Clicking a peer (or "connect node…") browses *that* node's
    API; peers serve plain **http** while this page is **https**, so a direct cross-origin fetch is
    blocked by the browser (mixed content). The explorer transparently relays those requests through this
    node's `GET /api/proxy` (read-only, SSRF-guarded — see
    [`../doc/15-rest-api.md`](../doc/15-rest-api.md)), so the browser only ever talks to its own origin;
  - **Supply** (`/api/supply`) — circulating supply + chain height.
  - **API** — a live, self-describing console: node status + every endpoint from the `/api` index, with
    a one-click "run" for GET endpoints (routed through the relay when browsing an http node).
  - `explorer/assets/bootstrap.min.css` is the vendored stylesheet.
- **`site/index.html`** (+ `site/assets/`) — the **bismuth.cz** landing page: the node install guide plus a
  link to the explorer.

The newer Tokens / Nodes / Supply views require a node running this repo's `rest_api.py` (the endpoints
were added here); older nodes return 404 for them until upgraded.

## Bootstrap snapshots

Snapshots are served **DB-direct** by the node itself (doc/43, `snapshot_p2p.py`): with
`snapshot_serve` on, `GET /api/snapshot/info` reports the ledger tip (height + tip hash) and
`GET /api/snapshot` streams a consistent, compacted LMDB image of the live ledger env straight to the
client (`env.copyfd` — no pre-built file, no doubled copy on disk). A fresh node with `bootstrap_p2p` on
fetches it from peers and verifies tip height + hash before trusting it.

## Deployment

nginx vhosts (`/etc/nginx/sites-available/{bismuth.cz,explorer.bismuth.cz}`) + Let's Encrypt certs; the
explorer's `fetch('/api/…')` is proxied by nginx to the node on `127.0.0.1:5659` (the node binds the
API to localhost; nginx is the only public edge). See [`../doc/00-overview.md`](../doc/00-overview.md)
for how this fits the whole system.
