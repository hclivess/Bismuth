# doc/43 — peer-to-peer ledger snapshot (decentralized rapid bootstrap)

**Status:** IMPLEMENTED and **ON by default** (`snapshot_serve` / `bootstrap_p2p` both default `true`).
Serving is **DB-DIRECT and on demand** — generated from the live LMDB ledger at request time, never from a
pre-built file. Complements doc/15's REST block-sync (`api_sync`): that streams individual blocks, this
serves the whole-chain **snapshot**. It is the only snapshot path: there is no central download and no
tarball.

## Why

A fresh node used to fetch the full ledger as a tarball from one central host — a single point of failure
and in practice a file that went **stale** (it sat ~92k blocks behind the chain) while doubling the ledger
into a second artifact on disk. That path is gone.

Any node **serves** its own always-current snapshot, and a fresh node **fetches + verifies** one from
peers.

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

- **The request path never scans the ledger.** `info` reads the tip; the stream is LMDB's own copy.

### Measured (mainnet, 2026-08-11)

| | |
|---|---|
| `/api/snapshot` | **9.7 GB streamed in 75 s** |
| downloaded bytes | open as a valid `BlockStore`: tip 4937625, tip hash byte-identical to the header |
| spot reads | heights 1 / 1,000,000 / 4,000,000 / tip all present |

## Fetching (`bootstrap_p2p`, in `chain_ops.bootstrap`)

On a fresh install (no `data.mdb`), unless `sync_from_genesis` is set: ask each candidate peer's
`/api/snapshot/info`, pick the **highest height**, stream `/api/snapshot` into
`<ledger env>.snapshot`, verify it, and move it into place as the ledger env. On any failure/mismatch the
download is discarded and the node seeds genesis and syncs from peers instead. Candidates come from `bootstrap_p2p_peers` (`["host:rest_port", …]`) or, failing that, the
known peers paired with this node's REST port.

Verification (`_verify_db_snapshot`) reopens the streamed env and fast-rejects it unless its tip height and
tip block hash match the advertised **tip_hash**, before the digester's full forward re-validation.

## Trust

The source is **untrusted-safe**. The advertised hash catches a corrupt or forged image, and the snapshot is
only a *starting point* — the digester re-validates every block (PoW, signatures, balances, hash linkage) as
the node syncs forward from the snapshot height. A malicious peer can at worst waste a download.

## Config

| flag | default | meaning |
|------|---------|---------|
| `snapshot_serve` | **`true`** | expose `/api/snapshot[/info]`, DB-direct from the live ledger |
| `bootstrap_p2p` | **`true`** | a fresh node fetches a verified snapshot from peers |
| `bootstrap_p2p_peers` | `[]` | optional explicit `host:rest_port` snapshot sources |

> **These MUST be assigned onto the node object in `node.py`.** `rest_api` and `chain_ops` read them as
> `getattr(node, "snapshot_serve", False)` etc., so when the assignment is missing the entire feature is
> **unreachable dead code** — `/api/snapshot` answers `"snapshot serving disabled"` and the P2P fetch is
> never attempted, no matter what the operator puts in the config. That was exactly the state until
> 2026-08-11. Flipping a default proves nothing here: **call the endpoint.**

## Bootstrapping a fresh node

A node with an empty ledger takes one of two paths (`chain_ops.bootstrap` / `seed_genesis`):

1. `bootstrap_p2p` (default **on**) — a DB-direct snapshot from peers, the fast path;
2. otherwise, or if no peer serves a valid snapshot, or with `sync_from_genesis` (doc/30) — seed the
   canonical genesis block (`ledger_genesis`) and sync from block 1, anchored by the hardcoded checkpoints.

A served snapshot is always the whole chain: the ledger env is the only copy of the chain the node has,
built block by block from genesis or itself installed from a complete snapshot.

## Post-hf2

Composes with a future **rolling** node (serves recent ranges; bootstraps its window from peers).

Tests: `tests/test_snapshot_db_direct.py` (on-demand stream, fetch + tip verification, tamper rejection,
highest-height selection).
