#!/usr/bin/env python3
"""doc/30 — discover candidate from-genesis validation exceptions.

Walks a ledger and reports the blocks a from-genesis VERIFYING replay would
choke on, emitting a ready-to-use validation_exceptions JSON file (the shape
``validation_exceptions.load`` consumes and the node serves to a syncing peer).

It detects the batch-scannable, state-free anomaly classes exactly:
  * DUPLICATE  — a signature that appears in more than one block (cross-block
                 replay) or twice within one block.
  * SIGNATURE  — a transaction whose signature fails cryptographic verification
                 (manual coin-rescue txs were often inserted unsigned / re-signed).

OVERSPEND / POW / TIMESTAMP cannot be found by a stateless scan (they need the
full balance/PoW replay). The reliable way to surface those is the iterative
real-sync method documented in doc/30 §"Discovering the heights": start a node
from genesis, let it HALT on the first ValueError, add that (height, check) to
the registry, restart, repeat — it uses the real consensus code and finds every
blocker in order. This tool front-loads the two classes that are cheap to batch.

SAFETY: this tool REFUSES to open the live production ledger
(``<repo>/static/ledger.db``). Run it against a COPY, or against a different
ledger file. Never scan the live prod ledger (it I/O-starves the running node).

Usage:
    python3 tools/find_validation_exceptions.py --ledger /path/to/ledger_copy.db \
        [--check-signatures] [--start 1] [--out exceptions.json]

    # make a safe copy first (node can stay running; sqlite backup is consistent):
    sqlite3 static/ledger.db ".backup /tmp/ledger_copy.db"
"""
import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import validation_exceptions as V

PROD_LEDGER = os.path.join(ROOT, "static", "ledger.db")


def _refuse_prod(path):
    """Hard stop if the target resolves to the live production ledger."""
    try:
        same = os.path.exists(path) and os.path.exists(PROD_LEDGER) and os.path.samefile(path, PROD_LEDGER)
    except OSError:
        same = False
    if same or os.path.realpath(path) == os.path.realpath(PROD_LEDGER):
        sys.exit("REFUSING to scan the live production ledger (%s). Copy it first:\n"
                 "  sqlite3 static/ledger.db \".backup /tmp/ledger_copy.db\"\n"
                 "then run against /tmp/ledger_copy.db." % PROD_LEDGER)


def _add(reg, height, check, reason, signature=None):
    e = reg.setdefault(int(height), {"checks": set(), "reason": reason, "signatures": None})
    e["checks"].add(check)
    if signature is not None:
        if e["signatures"] is None:
            e["signatures"] = set()
        e["signatures"].add(str(signature)[:24])   # short prefix is enough to pin the tx
    if reason not in e["reason"]:
        e["reason"] = (e["reason"] + "; " + reason) if e["reason"] else reason


def find_duplicates(conn, start):
    """Signatures present in >1 block (cross-block replay), and twice within a block.
    The waiver is recorded at the LATER occurrence — the one a replay reaches second."""
    reg = {}
    cur = conn.cursor()
    # cross-block: same signature at two different heights
    cur.execute("SELECT signature, MIN(block_height), MAX(block_height), COUNT(*) "
                "FROM transactions WHERE block_height >= ? AND signature != '' "
                "GROUP BY signature HAVING COUNT(DISTINCT block_height) > 1", (start,))
    for sig, lo, hi, cnt in cur.fetchall():
        _add(reg, hi, V.DUPLICATE, "cross-block replay of %s (first @%s)" % (str(sig)[:12], lo), sig)
    # in-block: same signature twice in one height
    cur.execute("SELECT block_height, signature, COUNT(*) c FROM transactions "
                "WHERE block_height >= ? AND signature != '' "
                "GROUP BY block_height, signature HAVING c > 1", (start,))
    for h, sig, c in cur.fetchall():
        _add(reg, h, V.DUPLICATE, "in-block duplicate signature", sig)
    return reg


def find_bad_signatures(conn, start):
    """Re-verify every transaction's signature; flag the failures. Pre-fork scheme
    only (post-fork content-txid verification would need the running node's
    fork_height). Slow — full crypto over the chain."""
    from polysign.signerfactory import SignerFactory
    reg = {}
    cur = conn.cursor()
    cur.execute("SELECT block_height, timestamp, address, recipient, amount, signature, public_key, "
                "operation, openfield, reward FROM transactions WHERE block_height >= ? "
                "ORDER BY block_height", (start,))
    checked = bad = 0
    for (h, ts, addr, rec, amt, sig, pub, op, of, reward) in cur:
        if reward and float(reward) != 0:
            continue                                   # coinbase row, not a signed spend
        if not sig:
            continue
        checked += 1
        try:
            SignerFactory.verify_tx_signature(False, "%.2f" % float(ts), str(addr), str(rec),
                                              "%.8f" % float(amt), str(op), str(of), str(sig), str(pub))
        except Exception as e:
            bad += 1
            _add(reg, h, V.SIGNATURE, "signature failed verification (%s)" % str(e)[:40], sig)
    sys.stderr.write("  signature scan: %d spends checked, %d failed\n" % (checked, bad))
    return reg


def _merge(into, other):
    for h, e in other.items():
        for c in e["checks"]:
            _add(into, h, c, e["reason"])
        if e["signatures"]:
            for s in e["signatures"]:
                into[h]["signatures"] = (into[h].get("signatures") or set())
                into[h]["signatures"].add(s)


def main():
    ap = argparse.ArgumentParser(description="Discover from-genesis validation exceptions (doc/30).")
    ap.add_argument("--ledger", required=True, help="path to a ledger COPY (never the live static/ledger.db)")
    ap.add_argument("--start", type=int, default=1, help="first height to scan (default 1)")
    ap.add_argument("--check-signatures", action="store_true",
                    help="also re-verify every signature (slow, pre-fork scheme)")
    ap.add_argument("--out", default="", help="write JSON here (default: stdout)")
    args = ap.parse_args()

    _refuse_prod(args.ledger)
    if not os.path.exists(args.ledger):
        sys.exit("ledger not found: %s" % args.ledger)

    conn = sqlite3.connect("file:%s?mode=ro" % args.ledger, uri=True)
    reg = {}
    sys.stderr.write("scanning duplicates...\n")
    _merge(reg, find_duplicates(conn, args.start))
    if args.check_signatures:
        sys.stderr.write("scanning signatures (slow)...\n")
        _merge(reg, find_bad_signatures(conn, args.start))
    conn.close()

    # serialize to the load() shape (sets -> sorted lists)
    out = {}
    for h in sorted(reg):
        e = reg[h]
        out[str(h)] = {"checks": sorted(e["checks"]), "reason": e["reason"],
                       "signatures": sorted(e["signatures"]) if e["signatures"] else None}
    blob = json.dumps(out, indent=2)
    if args.out:
        open(args.out, "w").write(blob + "\n")
        sys.stderr.write("wrote %d candidate exceptions -> %s\n" % (len(out), args.out))
    else:
        print(blob)
    sys.stderr.write("done. REVIEW before trusting — every entry LOOSENS a historical block.\n")


if __name__ == "__main__":
    main()
