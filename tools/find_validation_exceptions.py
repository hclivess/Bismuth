#!/usr/bin/env python3
"""doc/30 — discover candidate from-genesis validation exceptions from a ledger COPY.

Walks an LMDB ledger env (``block_store.BlockStore``) and reports the blocks a from-genesis VERIFYING
replay would choke on, emitting a ready-to-use validation_exceptions JSON file (the shape
``validation_exceptions.load`` consumes).

Detectors (all operate on a COPY, never the live ledger):
  * duplicate   — a signature in more than one block (cross-block replay) or twice
                  in one block. EXACT, from the ledger's signature index.
  * timestamp   — a block whose coinbase timestamp is <= the previous block's
                  (out-of-order / hand-inserted block). EXACT, block scan.
  * overspend   — an address whose lifetime credits (amount+reward received) minus
                  debits (amount+fee sent) is negative (manual coin-rescue / balance
                  edit). From the ledger's balance index. CANDIDATE — verify each hit
                  against the running node before trusting it.
  * signature   — a transaction whose signature fails cryptographic verification
                  (re-verified with the pre-fork scheme, which is what the whole
                  current mainnet chain uses). EXACT, parallel across cores.

NOT batch-detectable: PoW (Heavy3 is memory-hard — re-verifying millions of blocks
is infeasible). Surface any PoW waiver with the iterative real-sync method in
doc/30 instead.

SAFETY: refuses to open the live production ledger env (<repo>/static/blockstore-ledger.db).
Take a consistent online copy first (LMDB MVCC snapshot, safe while the node runs):
    python3 -c "import block_store as b; b.BlockStore('static/blockstore-ledger.db', readonly=True).store.copy_to('/tmp/ledger_copy')"

Usage:
    python3 tools/find_validation_exceptions.py --ledger /tmp/ledger_copy \
        [--duplicates] [--timestamps] [--overspend] [--check-signatures] \
        [--jobs N] [--start 1] [--out exceptions.json]
    (with no detector flags, runs duplicates+timestamps+overspend; signatures is
     opt-in because it is the slow one.)
"""
import argparse
import json
import os
import sys
from decimal import Decimal, ROUND_HALF_EVEN

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import block_store
import kvstore
import validation_exceptions as V

PROD_LEDGER = kvstore.store_path(os.path.join(ROOT, "static", "ledger.db"), "blockstore")
SCALE = Decimal(10) ** 8
Q8 = Decimal("0.00000001")
_PLACEHOLDERS = ("Development Reward", "Hypernode Payouts", "genesis")


def _refuse_prod(path):
    try:
        same = os.path.exists(path) and os.path.exists(PROD_LEDGER) and os.path.samefile(path, PROD_LEDGER)
    except OSError:
        same = False
    if same or os.path.realpath(path) == os.path.realpath(PROD_LEDGER):
        sys.exit("REFUSING to scan the live production ledger (%s). Copy it first (see --help / the module "
                 "docstring), then run against the copy." % PROD_LEDGER)


def _ro(path):
    return block_store.BlockStore(path, readonly=True)


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
def find_duplicates(store, start):
    """Walk the signature index (keys ``sig[:56] 0x00 height idx``, so equal signatures are adjacent) and
    confirm each multi-entry group against the full signature of the stored rows."""
    reg = {}
    sys.stderr.write("  [duplicate] signature index ...\n"); sys.stderr.flush()

    def flush(group, txn, cache):
        if len(group) < 2:
            return
        by_sig = {}
        for h, idx in group:
            r = store._row_at(txn, h, idx, cache)
            if r is not None:
                by_sig.setdefault(str(r[block_store.SIG]), []).append(h)
        for sig, hs in by_sig.items():
            if len(hs) < 2:
                continue
            heights = sorted(set(hs))
            if len(heights) > 1:
                _add(reg, heights[-1], V.DUPLICATE,
                     "cross-block replay of %s (first @%s)" % (sig[:12], heights[0]), sig)
            for h in heights:
                c = hs.count(h)
                if c > 1:
                    _add(reg, h, V.DUPLICATE, "in-block duplicate signature x%d" % c, sig)

    with store.store.txn() as txn:
        cur_prefix, group, cache = None, [], {}
        for k, _v in txn.iterate(store.sig):
            prefix = bytes(k[:-11])
            h, idx = block_store._split_pos(k)
            if prefix != cur_prefix:
                flush(group, txn, cache)
                cur_prefix, group = prefix, []
                if len(cache) > 4096:
                    cache.clear()
            if h >= start:
                group.append((h, idx))
        flush(group, txn, cache)
    return reg


# ------------------------------------------------------------------- timestamp ----
def find_timestamps(store, start, end):
    reg = {}
    sys.stderr.write("  [timestamp] scanning coinbase order ...\n"); sys.stderr.flush()
    # one coinbase (reward != 0) per block carries the block timestamp
    prev_h = prev_ts = None
    for h, rows in store.blocks_in_range(start, end):
        cb = next((r for r in reversed(rows) if block_store.is_coinbase_row(r)), None)
        if cb is None:
            continue
        ts = Decimal(str(cb[block_store.TS]))
        if prev_ts is not None and h == prev_h + 1 and ts <= prev_ts:
            _add(reg, h, V.TIMESTAMP, "block ts %s <= prev(%s) %s" % (ts, prev_h, prev_ts))
        prev_h, prev_ts = h, ts
    return reg


# ------------------------------------------------------------------- overspend ----
def find_overspends(store, start):
    """Exact net-balance footprint: an address whose lifetime credits (amount+reward
    received) minus debits (amount+fee sent) is NEGATIVE could only get there via a
    manual ledger edit — no validated spend can overspend. Read straight from the
    ledger's balance index. It reports the offending ADDRESSES (at the first block
    where each sends, a starting point); pinpointing the exact block needs the
    iterative real-sync (doc/30). Placeholder senders are excluded."""
    reg = {}
    sys.stderr.write("  [overspend] net-balance footprint ...\n"); sys.stderr.flush()
    found = 0
    for addr, (cred, rew, deb, fee) in store.iter_balances():
        units = cred + rew - deb - fee
        if units >= -1 or addr in _PLACEHOLDERS:
            continue
        found += 1
        first = store.address_rows(addr, role="sender", reverse=False, limit=1, lo=start)
        h = first[0][block_store.H] if first else start
        bal = Decimal(units) / SCALE
        _add(reg, h, V.OVERSPEND, "address %s nets %s (manual balance edit?)" % (str(addr)[:16], bal))
    sys.stderr.write("    %d real net-negative addresses (excluding placeholders)\n" % found)
    sys.stderr.flush()
    return reg


# ------------------------------------------------------------------- signatures ----
def _verify_worker(args):
    db_path, lo, hi = args
    sys.path.insert(0, ROOT)
    import digest_tx
    from polysign.signerfactory import SignerFactory
    from essentials import MAX_TX_SIGNATURE_LEN, MAX_TX_PUBKEY_LEN
    store = _ro(db_path)
    out = []
    try:
        for _h, rows in store.blocks_in_range(lo, hi - 1):
            for (h, ts, addr, rec, amt, sig, pub, _bh, _fee, _rew, op, of) in rows:
                if str(sig) in ("", "0"):
                    continue
                rts = "%.2f" % digest_tx.quantize_two(ts)
                ramt = "%.8f" % digest_tx.quantize_eight(amt)
                try:
                    SignerFactory.verify_tx_signature(
                        False, rts, str(addr)[:56], str(rec)[:56], ramt,
                        str(op)[:30], str(of)[:100000],
                        str(sig)[:MAX_TX_SIGNATURE_LEN], str(pub)[:MAX_TX_PUBKEY_LEN])
                except Exception as e:
                    out.append((h, str(sig)[:24], str(e)[:50]))
    finally:
        store.close()
    return out


def find_bad_signatures(db_path, start, end, jobs):
    import multiprocessing
    reg = {}
    step = max(1000, (end - start) // (jobs * 8) + 1)   # ~8 chunks per worker for load balance
    ranges = [(db_path, lo, min(lo + step, end + 1)) for lo in range(start, end + 1, step)]
    sys.stderr.write("  [signature] re-verifying %d height-chunks across %d workers ...\n"
                     % (len(ranges), jobs)); sys.stderr.flush()
    done = 0
    # spawn, not fork: an LMDB env must never be inherited across fork — each worker opens its own
    with multiprocessing.get_context("spawn").Pool(jobs) as p:
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
    ap.add_argument("--ledger", required=True,
                    help="path to an LMDB ledger env COPY (never the live static/blockstore-ledger.db)")
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

    store = _ro(args.ledger)
    end = store.tip()
    if end is None:
        sys.exit("empty ledger: %s" % args.ledger)
    sys.stderr.write("scanning %s (heights %d..%d)\n" % (args.ledger, args.start, end)); sys.stderr.flush()

    reg = {}
    if args.duplicates:
        _merge(reg, find_duplicates(store, args.start))
    if args.timestamps:
        _merge(reg, find_timestamps(store, args.start, end))
    if args.overspend:
        _merge(reg, find_overspends(store, args.start))
    store.close()
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
