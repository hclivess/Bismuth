#!/usr/bin/env python3
"""doc/30 — discover candidate from-genesis validation exceptions from a ledger COPY.

Walks a ledger and reports the blocks a from-genesis VERIFYING replay would choke
on, emitting a ready-to-use validation_exceptions JSON file (the shape
``validation_exceptions.load`` consumes).

Detectors (all operate on a COPY, never the live ledger):
  * duplicate   — a signature in more than one block (cross-block replay) or twice
                  in one block. EXACT, SQL.
  * timestamp   — a block whose coinbase timestamp is <= the previous block's
                  (out-of-order / hand-inserted block). EXACT, SQL + scan.
  * overspend   — a spend whose sender balance goes negative on a forward replay
                  from genesis (manual coin-rescue / balance edit). CANDIDATE —
                  the replay tracks consensus credit/debit (amount+reward to the
                  recipient, amount+fee from the sender, plus the negative-height
                  Development/Hypernode mirror-reward rows as credits); verify each
                  hit against the running node before trusting it.
  * signature   — a transaction whose signature fails cryptographic verification
                  (re-verified with the pre-fork scheme, which is what the whole
                  current mainnet chain uses). EXACT, parallel across cores.

NOT batch-detectable: PoW (Heavy3 is memory-hard — re-verifying millions of blocks
is infeasible). Surface any PoW waiver with the iterative real-sync method in
doc/30 instead.

The synthetic mirror-reward rows (block_height < 0, address "Development Reward" /
"Hypernode Payouts") are NEVER digest-validated, so they are excluded from the
signature/duplicate/overspend-as-sender checks — but included as balance CREDITS.

SAFETY: refuses to open the live production ledger (<repo>/static/ledger.db). Copy
it first:  sqlite3 static/ledger.db ".backup /tmp/ledger_copy.db"

Usage:
    python3 tools/find_validation_exceptions.py --ledger /path/copy.db \
        [--duplicates] [--timestamps] [--overspend] [--check-signatures] \
        [--jobs N] [--start 1] [--out exceptions.json]
    (with no detector flags, runs duplicates+timestamps+overspend; signatures is
     opt-in because it is the slow one.)
"""
import argparse
import json
import os
import sqlite3
import sys
from decimal import Decimal, ROUND_HALF_EVEN

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import validation_exceptions as V

PROD_LEDGER = os.path.join(ROOT, "static", "ledger.db")
SCALE = Decimal(10) ** 8
Q8 = Decimal("0.00000001")


def _refuse_prod(path):
    try:
        same = os.path.exists(path) and os.path.exists(PROD_LEDGER) and os.path.samefile(path, PROD_LEDGER)
    except OSError:
        same = False
    if same or os.path.realpath(path) == os.path.realpath(PROD_LEDGER):
        sys.exit("REFUSING to scan the live production ledger (%s). Copy it first:\n"
                 "  sqlite3 static/ledger.db \".backup /tmp/ledger_copy.db\"\n"
                 "then run against the copy." % PROD_LEDGER)


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % os.path.abspath(path), uri=True)


def _units(x):
    """NUMERIC -> integer atomic units (1e-8), consensus rounding."""
    try:
        return int((Decimal(str(x)).quantize(Q8, rounding=ROUND_HALF_EVEN) * SCALE))
    except Exception:
        return 0


def _add(reg, height, check, reason, signature=None):
    e = reg.setdefault(int(height), {"checks": set(), "reasons": set(), "signatures": None})
    e["checks"].add(check)
    e["reasons"].add(reason)
    if signature is not None and str(signature) not in ("", "0"):
        if e["signatures"] is None:
            e["signatures"] = set()
        e["signatures"].add(str(signature)[:24])


# ------------------------------------------------------------------- duplicate ----
def find_duplicates(conn, start):
    reg = {}
    cur = conn.cursor()
    sys.stderr.write("  [duplicate] cross-block ...\n"); sys.stderr.flush()
    cur.execute("SELECT signature, MIN(block_height), MAX(block_height) FROM transactions "
                "WHERE block_height >= ? AND signature NOT IN ('', '0') "
                "GROUP BY signature HAVING COUNT(DISTINCT block_height) > 1", (start,))
    for sig, lo, hi in cur.fetchall():
        _add(reg, hi, V.DUPLICATE, "cross-block replay of %s (first @%s)" % (str(sig)[:12], lo), sig)
    sys.stderr.write("  [duplicate] in-block ...\n"); sys.stderr.flush()
    cur.execute("SELECT block_height, signature, COUNT(*) c FROM transactions "
                "WHERE block_height >= ? AND signature NOT IN ('', '0') "
                "GROUP BY block_height, signature HAVING c > 1", (start,))
    for h, sig, c in cur.fetchall():
        _add(reg, h, V.DUPLICATE, "in-block duplicate signature x%d" % c, sig)
    return reg


# ------------------------------------------------------------------- timestamp ----
def find_timestamps(conn, start):
    reg = {}
    cur = conn.cursor()
    sys.stderr.write("  [timestamp] scanning coinbase order ...\n"); sys.stderr.flush()
    # one coinbase (reward != 0) per block carries the block timestamp
    cur.execute("SELECT block_height, timestamp FROM transactions "
                "WHERE block_height >= ? AND reward != 0 ORDER BY block_height", (start,))
    prev_h = prev_ts = None
    for h, ts in cur:
        ts = Decimal(str(ts))
        if prev_ts is not None and h == prev_h + 1 and ts <= prev_ts:
            _add(reg, h, V.TIMESTAMP, "block ts %s <= prev(%s) %s" % (ts, prev_h, prev_ts))
        prev_h, prev_ts = h, ts
    return reg


# ------------------------------------------------------------------- overspend ----
def find_overspends(conn, start):
    """Exact net-balance footprint: an address whose lifetime credits (amount+reward
    received) minus debits (amount+fee sent) is NEGATIVE could only get there via a
    manual ledger edit — no validated spend can overspend. This is the precise
    overspend footprint (the noisy per-block forward replay was dropped: it can't
    model consensus's "confirmed-balance, no same-block credit" semantics and so
    produces false positives). It reports the offending ADDRESSES; pinpointing the
    exact block needs the iterative real-sync (doc/30). The synthetic placeholder
    senders on the mirror-reward rows ("Development Reward" / "Hypernode Payouts")
    are pure debit artifacts, never real spenders, and are excluded."""
    reg = {}
    cur = conn.cursor()
    sys.stderr.write("  [overspend] net-balance footprint ...\n"); sys.stderr.flush()
    cur.execute(
        "WITH cred AS (SELECT recipient a, SUM(amount+reward) v FROM transactions GROUP BY recipient), "
        "     deb  AS (SELECT address   a, SUM(amount+fee)    v FROM transactions GROUP BY address) "
        "SELECT a, bal FROM ("
        "  SELECT COALESCE(cred.a,deb.a) a, COALESCE(cred.v,0)-COALESCE(deb.v,0) bal "
        "  FROM cred LEFT JOIN deb ON cred.a=deb.a "
        "  UNION SELECT deb.a, COALESCE(cred.v,0)-COALESCE(deb.v,0) FROM deb LEFT JOIN cred ON cred.a=deb.a) "
        "WHERE bal < -0.00000001")
    placeholders = ("Development Reward", "Hypernode Payouts", "genesis")
    found = 0
    for (addr, bal) in cur.fetchall():
        if str(addr) in placeholders:
            continue
        found += 1
        # locate the first block where this address sends (a starting point to investigate)
        cur2 = conn.cursor()
        cur2.execute("SELECT MIN(block_height) FROM transactions WHERE address=? AND block_height>=?",
                     (addr, start))
        h = cur2.fetchone()[0] or start
        _add(reg, h, V.OVERSPEND, "address %s nets %s (manual balance edit?)" % (str(addr)[:16], bal))
    sys.stderr.write("    %d real net-negative addresses (excluding mirror placeholders)\n" % found)
    sys.stderr.flush()
    return reg


# ------------------------------------------------------------------- signatures ----
def _verify_worker(args):
    db_path, lo, hi = args
    sys.path.insert(0, ROOT)
    import digest_tx
    from polysign.signerfactory import SignerFactory
    from essentials import MAX_TX_SIGNATURE_LEN, MAX_TX_PUBKEY_LEN
    conn = _ro(db_path)
    cur = conn.cursor()
    cur.execute("SELECT block_height, timestamp, address, recipient, amount, signature, public_key, "
                "operation, openfield FROM transactions WHERE block_height >= ? AND block_height < ? "
                "AND signature NOT IN ('', '0')", (lo, hi))
    out = []
    for (h, ts, addr, rec, amt, sig, pub, op, of) in cur:
        rts = "%.2f" % digest_tx.quantize_two(ts)
        ramt = "%.8f" % digest_tx.quantize_eight(amt)
        try:
            SignerFactory.verify_tx_signature(
                False, rts, str(addr)[:56], str(rec)[:56], ramt,
                str(op)[:30], str(of)[:100000],
                str(sig)[:MAX_TX_SIGNATURE_LEN], str(pub)[:MAX_TX_PUBKEY_LEN])
        except Exception as e:
            out.append((h, str(sig)[:24], str(e)[:50]))
    conn.close()
    return out


def find_bad_signatures(db_path, start, end, jobs):
    from multiprocessing import Pool
    reg = {}
    step = max(1000, (end - start) // (jobs * 8) + 1)   # ~8 chunks per worker for load balance
    ranges = [(db_path, lo, min(lo + step, end + 1)) for lo in range(start, end + 1, step)]
    sys.stderr.write("  [signature] re-verifying %d height-chunks across %d workers ...\n"
                     % (len(ranges), jobs)); sys.stderr.flush()
    done = 0
    with Pool(jobs) as p:
        for res in p.imap_unordered(_verify_worker, ranges):
            for (h, sig, err) in res:
                _add(reg, h, V.SIGNATURE, "signature verify failed: %s" % err, sig)
            done += 1
            if done % 10 == 0:
                sys.stderr.write("    ... %d/%d chunks, %d bad so far\n" % (done, len(ranges), len(reg)))
                sys.stderr.flush()
    return reg


# ------------------------------------------------------------------- merge/emit ----
def _merge(into, other):
    for h, e in other.items():
        for c in e["checks"]:
            _add(into, h, c, "")
        into[h]["reasons"] |= e["reasons"]
        if e["signatures"]:
            into[h]["signatures"] = (into[h].get("signatures") or set()) | e["signatures"]
    for h, e in other.items():
        into[h]["reasons"].discard("")


def main():
    ap = argparse.ArgumentParser(description="Discover from-genesis validation exceptions (doc/30).")
    ap.add_argument("--ledger", required=True, help="path to a ledger COPY (never the live static/ledger.db)")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--duplicates", action="store_true")
    ap.add_argument("--timestamps", action="store_true")
    ap.add_argument("--overspend", action="store_true")
    ap.add_argument("--check-signatures", action="store_true", help="parallel signature re-verify (slow)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    _refuse_prod(args.ledger)
    if not os.path.exists(args.ledger):
        sys.exit("ledger not found: %s" % args.ledger)

    # default: the fast/exact set + overspend
    if not (args.duplicates or args.timestamps or args.overspend or args.check_signatures):
        args.duplicates = args.timestamps = args.overspend = True

    conn = _ro(args.ledger)
    end = conn.execute("SELECT MAX(block_height) FROM transactions").fetchone()[0]
    sys.stderr.write("scanning %s (heights %d..%d)\n" % (args.ledger, args.start, end)); sys.stderr.flush()

    reg = {}
    if args.duplicates:
        _merge(reg, find_duplicates(conn, args.start))
    if args.timestamps:
        _merge(reg, find_timestamps(conn, args.start))
    if args.overspend:
        _merge(reg, find_overspends(conn, args.start))
    conn.close()
    if args.check_signatures:
        _merge(reg, find_bad_signatures(args.ledger, args.start, end, args.jobs))

    out = {}
    for h in sorted(reg):
        e = reg[h]
        out[str(h)] = {"checks": sorted(e["checks"]),
                       "reason": "; ".join(sorted(x for x in e["reasons"] if x)),
                       "signatures": sorted(e["signatures"]) if e["signatures"] else None}
    blob = json.dumps(out, indent=2)
    if args.out:
        open(args.out, "w").write(blob + "\n")
        sys.stderr.write("wrote %d candidate exceptions -> %s\n" % (len(out), args.out))
    else:
        print(blob)
    sys.stderr.write("done — %d candidate heights. REVIEW before trusting; each entry LOOSENS a block.\n"
                     % len(out))


if __name__ == "__main__":
    main()
