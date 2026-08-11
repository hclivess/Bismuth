#!/usr/bin/env python3
"""
Rebuild the LMDB block store from the SQLite ledger — the backfill the node itself does not do.

The node builds its block store FORWARD as it digests, so a store created after the chain already
existed only covers heights from the moment it was opened. This tool fills in all of history from
``static/ledger.db`` so the store holds the complete chain, and proves it byte-for-byte against
SQLite before anything is swapped in.

Safe to run against a LIVE node:
  * the ledger is opened READ-ONLY (``block_store._open_ro``) and SQLite is in WAL mode, so readers
    never block the node's writer;
  * it writes to a SEPARATE store path — LMDB allows exactly one writer per env, and the running node
    owns the live store's lock, so never point --out at the store the node has open;
  * run it under ``nice -n19 ionice -c3`` so the 23 GB read cannot starve the node (see the
    "no heavy scans on the prod ledger" rule).

Commands
  build    backfill [start..end] into --out (RESUMABLE: default start = store tip + 1)
  verify   assert every block in the store equals the SQLite rows exactly (block_store.verify_against_sqlite)
  compact  MVCC-consistent compacting copy of a store to --out (kvstore copy_to(compact=True))
  info     tip/size of a store and the ledger

Typical run:
  nice -n19 ionice -c3 python3 scripts/rebuild_block_store.py build  --out static/blockstore-rebuild.db
  nice -n19 ionice -c3 python3 scripts/rebuild_block_store.py verify --store static/blockstore-rebuild.db
  nice -n19 ionice -c3 python3 scripts/rebuild_block_store.py compact --store static/blockstore-rebuild.db \
                                                                      --out static/blockstore-compact.db
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import block_store  # noqa: E402


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _fmt(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f PB" % n


def _ledger_tip(ledger):
    conn = block_store._open_ro(ledger)
    try:
        return conn.execute("SELECT max(block_height) FROM transactions").fetchone()[0] or 0
    finally:
        conn.close()


def cmd_build(a):
    tip = _ledger_tip(a.ledger)
    store = block_store.BlockStore(a.out)
    try:
        start = a.start if a.start is not None else (store.tip() or 0) + 1
        end = a.end if a.end is not None else tip
        if start > end:
            print("nothing to do: store already at %s (ledger tip %s)" % (store.tip(), tip))
            return 0
        print("build %s -> %s   heights %d..%d (%d blocks), batch %d"
              % (a.ledger, a.out, start, end, end - start + 1, a.batch), flush=True)

        t0 = time.time()
        done = 0
        # Walk in chunks so progress/ETA is live and a crash resumes from the store tip.
        chunk = max(a.batch, a.progress)
        h = start
        while h <= end:
            hi = min(h + chunk - 1, end)
            n = block_store.build_from_sqlite(a.ledger, store, start=h, end=hi, batch_blocks=a.batch)
            done += n
            el = time.time() - t0
            rate = done / el if el > 0 else 0
            left = (end - hi) / rate if rate > 0 else 0
            print("  %d/%d  %.0f blk/s  elapsed %s  eta %s  store %s"
                  % (hi - start + 1, end - start + 1, rate,
                     _hms(el), _hms(left), _fmt(_dir_size(a.out))), flush=True)
            h = hi + 1
        print("DONE: %d blocks in %s; store tip %s, size %s"
              % (done, _hms(time.time() - t0), store.tip(), _fmt(_dir_size(a.out))), flush=True)
        return 0
    finally:
        store.close()


def _hms(s):
    s = int(s)
    return "%dh%02dm%02ds" % (s // 3600, (s % 3600) // 60, s % 60)


def _norm(v):
    """Normalize a stored field for VALUE comparison.

    The store can legitimately hold two flavours of the same row. Blocks written by the live node come
    from the wire tuple, where timestamp/amount/fee/reward are canonical STRINGS ('1786443820.50',
    '2.32955273'). Blocks backfilled by build_from_sqlite come back through SQLite, whose numeric column
    affinity coerced those same strings to REAL/INTEGER, so they read back as float/int. The VALUES are
    identical; only the Python type differs. A strict `==` therefore always fails on node-written blocks,
    which made verify unusable against a running node.
    Numbers are compared at the chain's OWN precision (8 decimals, quantize_eight). SQLite stores
    amount/fee/reward in REAL columns, so a canonical 8-dp value like '2.32944909' reads back as the
    float64 artifact 2.3294490899999998; full-precision float equality would call that a mismatch when
    the store in fact holds the exact consensus value and SQLite holds the lossy one.
    """
    if isinstance(v, (int, float)):
        return ("num", round(float(v), 8))
    if isinstance(v, str):
        try:
            return ("num", round(float(v), 8))
        except ValueError:
            return ("str", v)
    return ("str", v)


def _row_diff(got, want):
    """(type_only, value) counts of differing fields between two row lists."""
    type_only = value = 0
    for g, w in zip(got, want):
        for a, b in zip(g, w):
            if a == b:
                continue
            if _norm(a) == _norm(b):
                type_only += 1
            else:
                value += 1
    return type_only, value


def cmd_verify(a):
    store = block_store.BlockStore(a.store, readonly=True)
    try:
        end = a.end if a.end is not None else min(store.tip() or 0, _ledger_tip(a.ledger))
        start = a.start if a.start is not None else 1
        print("verify %s against %s   heights %d..%d%s"
              % (a.store, a.ledger, start, end, "  [values-only]" if a.values else ""), flush=True)
        t0 = time.time()
        if not a.values:
            blocks, txs = block_store.verify_against_sqlite(a.ledger, store, start=start, end=end)
            print("OK: %d blocks / %d txs byte-identical to SQLite in %s"
                  % (blocks, txs, _hms(time.time() - t0)), flush=True)
            return 0

        # values-only: tolerate the str-vs-float/int flavour difference (see _norm) but still fail hard on
        # any real value divergence, so this is usable against a live node without weakening the check.
        conn = block_store._open_ro(a.ledger)
        blocks = txs = type_only = 0
        bad = []
        try:
            cur = conn.cursor()
            for height, _bh, rows in block_store._grouped_blocks(cur, start, end, 500):
                got = store.get_block(height)
                if got is None:
                    bad.append((height, "missing from store"))
                    continue
                if len(got) != len(rows):
                    bad.append((height, "tx count %d != %d" % (len(got), len(rows))))
                    continue
                t, v = _row_diff(got, rows)
                type_only += t
                if v:
                    bad.append((height, "%d field(s) differ in VALUE" % v))
                blocks += 1
                txs += len(rows)
        finally:
            conn.close()
        el = _hms(time.time() - t0)
        if bad:
            for h, why in bad[:10]:
                print("  MISMATCH block %d: %s" % (h, why), flush=True)
            print("FAILED: %d block(s) differ in value (%d blocks / %d txs checked in %s)"
                  % (len(bad), blocks, txs, el), flush=True)
            return 1
        print("OK: %d blocks / %d txs match SQLite in VALUE in %s (%d field(s) differed only in "
              "str-vs-number flavour, which is expected: node writes the wire tuple, SQLite coerces)"
              % (blocks, txs, el, type_only), flush=True)
        return 0
    finally:
        store.close()


def cmd_compact(a):
    src_size = _dir_size(a.store)
    store = block_store.BlockStore(a.store, readonly=True)
    try:
        print("compact %s -> %s (source %s)" % (a.store, a.out, _fmt(src_size)), flush=True)
        t0 = time.time()
        os.makedirs(a.out, exist_ok=True)
        store.store.copy_to(a.out, compact=True)
    finally:
        store.close()
    dst_size = _dir_size(a.out)
    saved = src_size - dst_size
    print("DONE in %s: %s -> %s (saved %s, %.1f%%)"
          % (_hms(time.time() - t0), _fmt(src_size), _fmt(dst_size), _fmt(saved),
             (100.0 * saved / src_size) if src_size else 0.0), flush=True)
    return 0


def cmd_info(a):
    print("ledger %s tip %s (%s)" % (a.ledger, _ledger_tip(a.ledger), _fmt(os.path.getsize(a.ledger))))
    for p in a.stores:
        if not os.path.isdir(p):
            print("store  %s  (absent)" % p)
            continue
        s = block_store.BlockStore(p, readonly=True)
        try:
            print("store  %s  tip %s  size %s" % (p, s.tip(), _fmt(_dir_size(p))))
        finally:
            s.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", default="static/ledger.db", help="SQLite ledger (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="backfill the store from SQLite (resumable)")
    b.add_argument("--out", required=True, help="store path to WRITE (never the live node's store)")
    b.add_argument("--start", type=int, default=None, help="default: store tip + 1")
    b.add_argument("--end", type=int, default=None, help="default: ledger tip")
    b.add_argument("--batch", type=int, default=500, help="blocks per LMDB txn")
    b.add_argument("--progress", type=int, default=25000, help="blocks between progress lines")
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify", help="byte-compare the store against SQLite")
    v.add_argument("--store", required=True)
    v.add_argument("--start", type=int, default=None)
    v.add_argument("--end", type=int, default=None)
    v.add_argument("--values", action="store_true",
                   help="compare VALUES, tolerating the str-vs-number flavour difference between "
                        "node-written and SQLite-backfilled rows (use against a live node)")
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("compact", help="compacting copy of a store")
    c.add_argument("--store", required=True)
    c.add_argument("--out", required=True)
    c.set_defaults(func=cmd_compact)

    i = sub.add_parser("info", help="tips and sizes")
    i.add_argument("stores", nargs="*", default=[])
    i.set_defaults(func=cmd_info)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
