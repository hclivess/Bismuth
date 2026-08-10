#!/usr/bin/env python3
"""
Create a CONSISTENT ledger snapshot WHILE the node is running — and verify it before trusting it.

Why this exists: a plain ``cp ledger.db`` (or tar) of a live, WAL-mode SQLite database is the classic
way to capture a CORRUPT or half-written snapshot — the file is mid-transaction and the -wal hasn't been
folded in. That is the "snapshots have always had issues" bug.

This uses SQLite's ONLINE BACKUP API (``sqlite3.Connection.backup``), which copies a transactionally
CONSISTENT image even while the node keeps writing (it re-copies any pages that change mid-backup), then
runs ``PRAGMA integrity_check`` on each snapshot, and only then packages the bootstrap tarball. The node
never has to stop.

Usage:
    python3 scripts/snapshot.py                      # snapshot ./static -> /tmp/ledger.tar.gz
    python3 scripts/snapshot.py --static static --tarball /var/www/bismuth.cz/ledger.tar.gz
    python3 scripts/snapshot.py --dbs ledger.db,hyper.db,index.db --keep   # keep the loose .db files
"""
import argparse
import os
import sqlite3
import tarfile
import time


def snapshot_db(src, dst):
    """Online-backup ``src`` (a live DB) to ``dst``, then integrity-check ``dst``. Raises on a bad copy."""
    # The live DB is opened normally; WAL mode allows our reader alongside the node's writer. The backup
    # API folds the WAL and re-copies pages the node mutates during the copy, so the result is consistent.
    src_conn = sqlite3.connect(src, timeout=120)
    dst_conn = sqlite3.connect(dst)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    # Trust nothing: verify the snapshot is a structurally valid, self-consistent database.
    check = sqlite3.connect(dst)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        raise RuntimeError("snapshot integrity_check FAILED for %s: %s" % (dst, result))


def snapshot_lmdb(src_dir, dst_dir, backend="lmdb"):
    """Online, consistent copy of a KV store — the POST-HARDFORK stores (the LMDB block store, the
    balance index). Routed through the engine-agnostic KV seam (``kvstore.open_store`` -> ``KVStore.copy_to``,
    doc/26 storage stage 1) so the snapshot tool is engine-independent: the LMDB backend takes an MVCC
    ``env.copy`` snapshot (consistent even while the node writes, a raw cp would capture a mid-write mmap),
    and copy_to drops free pages (compact=True) so the bootstrap is small. Swapping the engine here is a
    one-arg ``backend=`` change. Leaves the copy at ``dst_dir`` in the backend's native layout."""
    from kvstore import open_store
    # readonly + lock=False: a second reader alongside the node's writer (lock=False is the snapshot tool's
    # standalone-reader convention — it never registers in LMDB's reader table). dbs=[] so we open the env
    # WITHOUT touching named sub-DBs (copy_to duplicates the whole env regardless of which sub-DBs we name,
    # so the prior max_dbs=16 / blocks+pubkeys enumeration is unnecessary).
    store = open_store(backend, src_dir, dbs=[], readonly=True, lock=False)
    try:
        store.copy_to(dst_dir, compact=True)
    finally:
        store.close()


def main():
    ap = argparse.ArgumentParser(description="Consistent, verified ledger snapshot of a running node.")
    ap.add_argument("--static", default="static", help="node static dir holding the DBs (default: static)")
    ap.add_argument("--out", default="/tmp/bismuth-snapshot", help="scratch dir for the loose snapshots")
    ap.add_argument("--tarball", default="/tmp/ledger.tar.gz", help="output bootstrap tarball")
    ap.add_argument("--dbs", default="ledger.db,hyper.db,index.db",
                    help="comma-separated SQLite DB filenames to include (skips any that are absent)")
    ap.add_argument("--stores", default="blockstore-ledger.db,balanceindex-ledger.db",
                    help="comma-separated LMDB store dirs — the POST-HARDFORK stores (block store, "
                         "balance index). They are namespaced per ledger (<name>-<ledger filename>, "
                         "kvstore.store_path) so networks never share one; skips any that are absent")
    ap.add_argument("--keep", action="store_true", help="keep the loose snapshots (default: remove)")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    made = []  # (path_on_disk, arcname_in_tarball) so a bootstrapping node extracts to the right place

    # SQLite ledger (covers all blocks up to the tip, pre- AND post-hardfork)
    for db in [d.strip() for d in a.dbs.split(",") if d.strip()]:
        src = os.path.join(a.static, db)
        if not os.path.exists(src):
            print("skip %s (not present)" % db, flush=True)
            continue
        dst = os.path.join(a.out, db)
        t0 = time.time()
        print("snapshotting %s (online backup, node may keep running)…" % db, flush=True)
        snapshot_db(src, dst)
        print("  ok  %s  %.1f MB  %.1fs  integrity:ok" % (db, os.path.getsize(dst) / 1e6, time.time() - t0),
              flush=True)
        made.append((dst, db))

    # POST-HARDFORK LMDB stores (block store, balance index) — included when the node has them enabled
    for store in [s.strip() for s in a.stores.split(",") if s.strip()]:
        src = os.path.join(a.static, store)
        if not os.path.isdir(src) or not os.path.exists(os.path.join(src, "data.mdb")):
            print("skip %s (post-hardfork store not present)" % store, flush=True)
            continue
        dst = os.path.join(a.out, store)
        t0 = time.time()
        print("snapshotting %s (LMDB online copy, post-hardfork store)…" % store, flush=True)
        snapshot_lmdb(src, dst)
        size = os.path.getsize(os.path.join(dst, "data.mdb")) / 1e6
        print("  ok  %s  %.1f MB  %.1fs" % (store, size, time.time() - t0), flush=True)
        made.append((dst, store))

    if not made:
        raise SystemExit("no DBs or stores found under %s" % a.static)

    print("packaging %s…" % a.tarball, flush=True)
    tmp_tar = a.tarball + ".part"
    with tarfile.open(tmp_tar, "w:gz") as tar:
        for path, arc in made:
            tar.add(path, arcname=arc)
    os.replace(tmp_tar, a.tarball)   # atomic publish: readers never see a half-written tarball
    print("snapshot ready: %s  (%.1f MB)" % (a.tarball, os.path.getsize(a.tarball) / 1e6))

    # doc/43: write the P2P-snapshot manifest (height + sha256 + size) that REST /api/snapshot/info serves.
    _height = 0
    _led = os.path.join(a.out, "ledger.db")
    if os.path.exists(_led):
        _c = sqlite3.connect(_led)
        try:
            _row = _c.execute("SELECT max(block_height) FROM transactions").fetchone()
            _height = (_row[0] if _row else 0) or 0
        finally:
            _c.close()
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import snapshot_p2p
        _m = snapshot_p2p.write_manifest(a.tarball, _height)
        print("manifest: %s  (height %s, sha256 %s)" % (snapshot_p2p.manifest_path(a.tarball), _m["height"], _m["sha256"][:12]))
    except Exception as _e:
        print("WARN: manifest not written (%s) — /api/snapshot/info will report unavailable" % _e)

    if not a.keep:
        import shutil
        for path, _ in made:
            try:
                shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
