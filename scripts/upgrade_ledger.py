#!/usr/bin/env python3
"""Upgrade an LMDB block store to the full ledger schema (block_store.SCHEMA).

A store built before the ledger became LMDB-only holds the blocks but none of what the node now keeps in
the same environment: the per-block difficulty record and the signature / address / balance / txid /
pubkey indexes. This rebuilds them from the blocks alone:

  1. difficulty history — the retarget is a recurrence on the previous recorded difficulty, so it is
     replayed forward from block timestamps (difficulty.retarget, the node's own code). It is anchored on
     exact difficulties published by peers (``GET /api/stats/difficulty`` on each ``--anchor`` REST node):
     the replay starts at the first anchor and is re-anchored at every later one, and every anchor the
     replay reaches is reported as an exact match or a mismatch.
  2. every derived index (block_store.BlockStore.reindex).

Usage:
    python3 scripts/upgrade_ledger.py static/blockstore-ledger.db --anchor http://185.100.232.5:5659

Run it with the node stopped (LMDB allows one writer). Idempotent: re-running rebuilds the same state.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import block_store  # noqa: E402
import difficulty as D  # noqa: E402
from kvstore import Codec  # noqa: E402


def fetch_anchors(urls, timeout=30):
    anchors = {}
    for u in urls:
        u = u.rstrip("/")
        try:
            with urllib.request.urlopen(u + "/api/stats/difficulty", timeout=timeout) as r:
                series = json.loads(r.read().decode())["series"]
        except Exception as e:
            print(f"anchor source {u} unavailable: {e}")
            continue
        for p in series:
            anchors.setdefault(int(p["height"]), float(p["difficulty"]))
    return anchors


def _coinbase_ts_fast(rec):
    """Mining-tx timestamp straight from a stored record (no pubkey expansion)."""
    for t in reversed(rec["t"]):
        if isinstance(t, (bytes, bytearray, memoryview)):
            import txrec
            row = txrec.unpack_row(bytes(t))
            ts, reward = row[0], row[8]
        else:
            ts, reward = t[0], t[8]
        if block_store.numeric(reward) != 0:
            return block_store.numeric(ts)
    return None


def rebuild_difficulty(store, anchors, log=print, batch=20000):
    tip = store.tip()
    usable = sorted(h for h in anchors if h <= tip)
    if not usable:
        raise SystemExit("no difficulty anchor at or below the store tip — cannot rebuild difficulty")
    start = usable[0]
    ts = {}
    d = None
    pending, matched, mismatched = [], 0, []
    t0 = time.time()
    with store.store.txn() as txn:
        it = txn.range(store.blocks, start=Codec.hkey(max(1, start - D.DIFFICULTY_WINDOW - 2)))
        for k, v in it:
            h = Codec.unhkey(k)
            ts[h] = _coinbase_ts_fast(Codec.unpack(v))
            ts.pop(h - D.DIFFICULTY_WINDOW - 5, None)
            if h < start:
                continue
            if h == start:
                d = Decimal(block_store.real_text(anchors[h]))
            else:
                p = h - 1
                lo = max(1, p - D.DIFFICULTY_WINDOW)
                tl = Decimal(ts[p])
                tbl = Decimal(ts[p - 1]) if (p - 1) in ts else tl
                t1441 = Decimal(ts[lo])
                t1440 = Decimal(ts[lo + 1]) if lo + 1 <= p else t1441
                nd, *_ = D.retarget(d, tl, tbl, t1441, t1440)
                if nd < D.MIN_DIFFICULTY:
                    nd = D.MIN_DIFFICULTY
                d = Decimal(block_store.real_text(D.recorded(nd)))
                if h in anchors:
                    if float(d) == anchors[h]:
                        matched += 1
                    else:
                        mismatched.append((h, str(d), anchors[h]))
                        d = Decimal(block_store.real_text(anchors[h]))
            pending.append((h, str(d)))
            if len(pending) >= batch:
                store.put_difficulties(pending)
                pending = []
                log(f"difficulty {h}/{tip} ({time.time() - t0:.0f}s)")
    if pending:
        store.put_difficulties(pending)
    log(f"difficulty rebuilt {start}..{tip}: {matched} anchors exact, {len(mismatched)} re-anchored")
    for m in mismatched:
        log(f"  re-anchored at {m[0]}: replay {m[1]} != published {m[2]}")
    return matched, mismatched


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("store", help="block store directory (e.g. static/blockstore-ledger.db)")
    ap.add_argument("--anchor", action="append", default=[], help="REST node base URL for difficulty anchors")
    ap.add_argument("--fork-height", type=int, default=None)
    ap.add_argument("--skip-difficulty", action="store_true")
    args = ap.parse_args()

    store = block_store.BlockStore(args.store)
    print(f"store {args.store}: tip {store.tip()}, schema {store.schema()}")
    if not args.skip_difficulty:
        anchors = fetch_anchors(args.anchor)
        print(f"{len(anchors)} difficulty anchors")
        rebuild_difficulty(store, anchors)
    t0 = time.time()
    n = store.reindex(fork_height=args.fork_height,
                      progress=lambda h, tip: (h % 100000 < 2000) and print(f"index {h}/{tip} ({time.time() - t0:.0f}s)", flush=True))
    store.set_schema()
    print(f"indexed {n} txs, {store.address_count()} addresses in {time.time() - t0:.0f}s; schema {store.schema()}")
    store.close()


if __name__ == "__main__":
    main()
