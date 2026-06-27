# doc/43 — peer-to-peer ledger snapshot (decentralized rapid bootstrap)

**Status:** IMPLEMENTED, OFF by default. Complements doc/15's REST block-sync (`api_sync`) — that streams
blocks; this serves the whole-ledger **snapshot** from peers, the P2P equivalent of the central
`bootstrap_url` download.

## Why

`chain_ops.bootstrap` fetches the full ledger from a single configurable host (`bootstrap_url` →
`bismuth.cz/ledger.tar.gz`). That's a single point of failure and a central dependency. This lets any node
**serve** its snapshot and a fresh node **fetch + verify** one from peers instead.

## Pieces (all gated, default off)

- **Build (`scripts/snapshot.py`)** — already makes a *consistent* tarball (SQLite online-backup +
  `integrity_check`, LMDB MVCC copy). It now also writes a **manifest** sidecar
  `<tarball>.manifest.json` = `{height, sha256, size, tarball}` (`snapshot_p2p.write_manifest`).
- **Serve (`node.snapshot_serve`, REST)** —
  - `GET /api/snapshot/info` → the manifest (or `{"available": false}`).
  - `GET /api/snapshot` → streams the pre-built tarball (chunked, `application/octet-stream`; already
    gzipped so no HTTP re-compress; `X-Bismuth-Snapshot-{Height,Sha256}` headers).
  - **The serve path only ever streams an already-built file — it never reads or scans the live ledger on
    a request** (that work is `scripts/snapshot.py`, run out-of-band, e.g. on a cron).
- **Fetch (`node.bootstrap_p2p`, in `chain_ops.bootstrap`)** — before the central download: ask each
  candidate peer's `/api/snapshot/info`, pick the **highest height**, download `/api/snapshot`, and
  **verify the sha256** against the advertised manifest. On any failure/mismatch it discards the file and
  falls back to `bootstrap_url`. Candidate peers come from `bootstrap_p2p_peers` (`["host:rest_port", …]`)
  or, failing that, the known peers paired with this node's REST port.

## Trust

The source is **untrusted-safe**: the sha256 catches a corrupt/forged tarball, and the snapshot is just a
starting point — the digester re-validates every block (PoW, signatures, balances, hash linkage) as the
node syncs forward from the snapshot height. A malicious peer can at worst waste a download.

## Config

| flag | default | meaning |
|------|---------|---------|
| `snapshot_serve` | `false` | expose `/api/snapshot[/info]` |
| `snapshot_path` | `static/ledger-snapshot.tar.gz` | the pre-built tarball to serve (+ its `.manifest.json`) |
| `bootstrap_p2p` | `false` | try peers (verified) before `bootstrap_url` |
| `bootstrap_p2p_peers` | `[]` | optional explicit `host:rest_port` snapshot sources |

## Post-hf2

hf2 forces a fleet upgrade, so REST-capable peers become common. Recommended then: default `snapshot_serve`
on for full nodes (cheap — streams a file) and `bootstrap_p2p` on, making the central host a fallback rather
than the primary. Composes with a future **rolling** node (serves recent ranges; bootstraps its window from
peers).

Tests: `tests/test_snapshot_p2p.py` (manifest round-trip + stale guard, fetch+verify via a stub peer,
tamper rejection, highest-height selection).
