# web/ — deployed front-ends

Static front-ends served by this server's nginx, version-controlled here (no build step — vanilla JS +
vendored Bootstrap).

- **`explorer/index.html`** — the Bismuth block explorer, deployed at **https://explorer.bismuth.cz**.
  A single page over the node's read-only REST API (`/api/`, proxied same-origin by nginx):
  - live status + **circulating supply**, block paging, block / transaction / address / balance lookup;
  - **Tokens** (`/api/tokens`, `/api/token/{name}`) — per-token supply, holders, balances;
  - **Nodes** (`/api/nodes`) — browse the peer network: each node's height, version, **reputation**, and
    connected / banned / whitelisted status;
  - **Supply** (`/api/supply`) — circulating supply + chain height.
  - `explorer/assets/bootstrap.min.css` is the vendored stylesheet.
- **`site/index.html`** (+ `site/assets/`) — the **bismuth.cz** landing page: the bootstrap-snapshot
  download (`ledger.tar.gz`) plus a link to the explorer.

The newer Tokens / Nodes / Supply views require a node running this repo's `rest_api.py` (the endpoints
were added here); older nodes return 404 for them until upgraded.

## Bootstrap snapshots

`ledger.tar.gz` is produced by [`../scripts/snapshot.py`](../scripts/snapshot.py), which takes a
**consistent, integrity-checked** snapshot of the **running** node (SQLite online-backup for the ledger
DBs + LMDB `env.copy` for the post-hardfork block store / balance index) and publishes the tarball
atomically — so it is safe to run on the live node without stopping it. Example:

```
python3 scripts/snapshot.py --static static --tarball /var/www/bismuth.cz/ledger.tar.gz
```

## Deployment

nginx vhosts (`/etc/nginx/sites-available/{bismuth.cz,explorer.bismuth.cz}`) + Let's Encrypt certs; the
explorer's `fetch('/api/…')` is proxied by nginx to the node on `127.0.0.1:5659` (the node binds the
API to localhost; nginx is the only public edge). See [`../doc/00-overview.md`](../doc/00-overview.md)
for how this fits the whole system.
