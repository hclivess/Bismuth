# web/ — deployed front-ends

Static front-ends served by this server's nginx, version-controlled here.

- **`explorer/index.html`** — the Bismuth block explorer, deployed at **https://explorer.bismuth.cz**.
  A single page over the node's read-only REST API (`/api/`, proxied same-origin by nginx): live status,
  block paging, and block / transaction / address / balance lookup. No build step — vanilla JS +
  Bootstrap.
- **`site-index.html`** — the **bismuth.cz** landing page: the bootstrap-snapshot download
  (`ledger.tar.gz`) plus a link to the explorer.

Deployment: nginx vhosts (`/etc/nginx/sites-available/{bismuth.cz,explorer.bismuth.cz}`) + Let's
Encrypt certs; the explorer's `fetch('/api/…')` is proxied by nginx to the node on `127.0.0.1:5659`.
See [`../doc/00-overview.md`](../doc/00-overview.md) for how this fits the whole system.
