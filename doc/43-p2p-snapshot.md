# doc/43 — peer-to-peer ledger snapshot (decentralized rapid bootstrap)

**Status:** IMPLEMENTED and **ON by default** (`snapshot_serve` / `bootstrap_p2p` both default `true`).
Serving is **DB-DIRECT and on demand** — generated from the live LMDB block store at request time, never
from a pre-built file. Complements doc/15's REST block-sync (`api_sync`): that streams individual blocks,
this serves the whole-chain **snapshot**, replacing the central `bootstrap_url` download.

## Why

`chain_ops.bootstrap` used to fetch the full ledger from one configurable host
(`bootstrap_url` → `bismuth.cz/ledger.tar.gz`) — a single point of failure, a central dependency, and in
practice a file that goes **stale**: the hosted tarball sat at height 4,845,489 while the chain was at
4,937,xxx, ~92k blocks behind, and it doubled 23 GB of ledger into a second 5.9 GB artifact on disk.

Any node now **serves** its own always-current snapshot, and a fresh node **fetches + verifies** one from
peers. The hosted tarball was deleted; see "Bootstrapping a fresh node" below for what that implies.

## Serving — DB-direct, on demand (the primary path)

No pre-built file, no doubled ledger, never stale:

- `GET /api/snapshot/info` → `snapshot_p2p.db_snapshot_info(node)` reads **only the store's tip** —
  O(1), no scan, no file:

  ```json
  {"available": true, "db_direct": true, "format": "lmdb",
   "height": 4937625, "tip_hash": "78789b7fdb3081fe362e33d9f39e7e5672cd4948737762ca34b500c0"}
  ```

- `GET /api/snapshot` → `snapshot_p2p.stream_db_snapshot()` → `block_store.stream_snapshot()` →
  **`env.copyfd`**: a consistent, **compacted** MVCC image written straight to the response socket.
  Consistent *even while the node writes*, because LMDB's MVCC gives the copy a stable read snapshot.

  Implementation notes that matter:
  * the size is not known ahead of time (it is compacted on the fly), so there is **no `Content-Length`**
    — end-of-body is signalled by closing the connection;
  * the HTTP headers must be **flushed before** `copyfd`, which writes the raw fd and would otherwise
    race ahead of still-buffered header bytes;
  * headers carry `X-Bismuth-Snapshot-{Height,Format,Tip-Hash}`.

- **The request path never scans the SQLite ledger.** It reads the LMDB store only.

**Fallback:** if the node has no streamable block store, both endpoints fall back to the legacy pre-built
tarball + `.manifest.json` sidecar (`{height, sha256, size, tarball}`, written by `scripts/snapshot.py`).
That path streams an already-built file and likewise never scans the live ledger.

### Measured (mainnet, 2026-08-11)

| | |
|---|---|
| `/api/snapshot` | **9.7 GB streamed in 75 s** |
| downloaded bytes | open as a valid `BlockStore`: tip 4937625, tip hash byte-identical to the header |
| spot reads | heights 1 / 1,000,000 / 4,000,000 / tip all present |

## Fetching (`bootstrap_p2p`, in `chain_ops.bootstrap`)

Before the central download: ask each candidate peer's `/api/snapshot/info`, pick the **highest height**,
download `/api/snapshot`, and verify it. On any failure/mismatch it discards the download and falls back to
`bootstrap_url`. Candidates come from `bootstrap_p2p_peers` (`["host:rest_port", …]`) or, failing that, the
known peers paired with this node's REST port.

Verification differs by format: the legacy tarball is checked against the manifest **sha256**; a db_direct
stream is fast-rejected against the advertised **tip_hash** before the digester's full forward re-validation.

## Trust

The source is **untrusted-safe**. The advertised hash catches a corrupt or forged image, and the snapshot is
only a *starting point* — the digester re-validates every block (PoW, signatures, balances, hash linkage) as
the node syncs forward from the snapshot height. A malicious peer can at worst waste a download.

## Config

| flag | default | meaning |
|------|---------|---------|
| `snapshot_serve` | **`true`** | expose `/api/snapshot[/info]` (DB-direct when a block store is present) |
| `bootstrap_p2p` | **`true`** | try peers (verified) before `bootstrap_url` |
| `snapshot_path` | `static/ledger-snapshot.tar.gz` | legacy pre-built tarball (+ `.manifest.json`) — fallback only |
| `bootstrap_p2p_peers` | `[]` | optional explicit `host:rest_port` snapshot sources |

> **These four MUST be assigned onto the node object in `node.py`.** `rest_api` and `chain_ops` read them as
> `getattr(node, "snapshot_serve", False)` etc., so when the assignment is missing the entire feature is
> **unreachable dead code** — `/api/snapshot` answers `"snapshot serving disabled"` and the P2P fetch is
> never attempted, no matter what the operator puts in the config. That was exactly the state until
> 2026-08-11. Flipping a default proves nothing here: **call the endpoint.**

## Bootstrapping a fresh node

The hosted `bismuth.cz/ledger.tar.gz` has been **deleted**, so the built-in `bootstrap_url` default 404s and
`chain_ops.bootstrap` **raises** rather than degrading. A node with an empty ledger needs one of:

1. `bootstrap_p2p` (default **on**) — fetch from peers, the intended path;
2. `sync_from_genesis` (doc/30) — seed canonical genesis and sync from block 1, anchored by the hardcoded
   checkpoints;
3. `bootstrap_file` / a local `<ledger_path>.tar.gz`.

## Prerequisite: a complete block store

DB-direct serving is only meaningful once the store holds the **whole chain**. The node builds its store
FORWARD as it digests and has no backfill, so a store created after the chain already existed covers only
heights since it was opened — serving that would hand a peer a store with a hole. Backfill first with
`scripts/rebuild_block_store.py` (doc/26); mainnet was backfilled and byte-verified on 2026-08-11.

## Post-hf2

Both flags being on by default already makes the central host a fallback rather than the primary. Composes
with a future **rolling** node (serves recent ranges; bootstraps its window from peers).

Tests: `tests/test_snapshot_p2p.py` (manifest round-trip + stale guard, fetch+verify via a stub peer, tamper
rejection, highest-height selection), `tests/test_snapshot_db_direct.py`, `tests/test_snapshot_kvstore.py`.
