# attic/

Retired and orphaned files, kept for reference — **not part of the running node**. Nothing in the
node, tests, or tools imports anything here (verified by import-graph analysis); these are abandoned
alternates, one-off scripts, and stale demos parked out of the way. They may be broken or out of date.
Resurrect into the repo root if a real use reappears; otherwise they rot here. See the repository
reorganization item in [`doc/17-roadmap.md`](../doc/17-roadmap.md).

Current contents:

- `hyperlane_asyncio.py` — abandoned asyncio variant of the (stub) hyperlane manager; broken on
  Python ≥ 3.10 (`asyncio.get_event_loop()` at class scope). The live stub is `../hyperlane.py`.
- `demo_getstatus.py`, `demo_getaddresssince.py` — single-command client demos of **legacy socket**
  commands, superseded by the REST API (see [`doc/15`](../doc/15-rest-api.md)).
- `rewards_reindex.py` — one-off dev-reward backfill script.
- `rewards_test.py` — ad-hoc ledger sanity script (not a pytest test; it pokes `static/ledger.db`).
