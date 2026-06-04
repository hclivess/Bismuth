"""
Replay-verify a Bismuth ledger: recompute every block hash from the stored rows through the frozen
consensus boundary (bismuth_serialize.block_hash) and compare to the stored hash.

Two modes:
  * as-stored          -> chain-integrity check (the serialization reproduces the stored hashes);
  * integer round-trip -> recompute with each amount passed through amounts.to_units/from_units,
                          proving the planned integer-amount storage migration (doc/16 phase 2) does
                          NOT change any block hash. This is the safety gate for that migration:
                          run it before and after, and require zero mismatches.

Run on a ledger file:
    python3 replay_verify.py [static/ledger.db]
Exit code is non-zero if any block hash does not reproduce.
"""
import bismuth_serialize
import amounts

# Raw transactions-row column indexes.
H, TS, ADDR, RECIP, AMOUNT, SIG, PUBKEY, BLOCKHASH, FEE, REWARD, OP, OPENFIELD = range(12)


def verify_blocks(rows, integer_roundtrip=False):
    """`rows` is an ordered list of raw 12-field transaction tuples (ascending block_height, then
    insertion order). Returns a list of mismatch dicts (empty == all good). The first block present
    is used only as the previous-hash anchor (its own hash can't be checked without its predecessor).
    """
    blocks = []
    by_height = {}
    for r in rows:
        if r[H] <= 0:           # skip negative-height "mirror" reward rows (not part of block hashes)
            continue
        if r[H] not in by_height:
            by_height[r[H]] = []
            blocks.append(r[H])
        by_height[r[H]].append(r)

    mismatches = []
    prev_hash = None
    for i, height in enumerate(blocks):
        brows = by_height[height]
        stored = brows[0][BLOCKHASH]
        if i == 0:
            prev_hash = stored  # anchor only
            continue
        tx_list = []
        for r in brows:
            # Reproduce the exact string forms digest hashed. The legacy NUMERIC column affinity
            # coerces timestamp/amount back to float/int on read, so reformat them to their canonical
            # '%.2f' / '%.8f' strings; the text fields come back as strings. (This lossy storage is
            # precisely what phase 2 replaces with integer amounts + explicit text columns.)
            timestamp = "%.2f" % float(r[TS])
            if integer_roundtrip:
                amount = amounts.from_units(amounts.to_units(r[AMOUNT]))
            else:
                amount = "%.8f" % float(r[AMOUNT])
            tx_list.append((timestamp, str(r[ADDR]), str(r[RECIP]), amount,
                            str(r[SIG]), str(r[PUBKEY]), str(r[OP]), str(r[OPENFIELD])))
        computed = bismuth_serialize.block_hash(tx_list, prev_hash)
        if computed != stored:
            mismatches.append({"height": height, "stored": stored, "computed": computed})
        prev_hash = stored
    return mismatches


def _main():
    import sqlite3
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "static/ledger.db"
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("SELECT block_height,timestamp,address,recipient,amount,signature,public_key,"
                "block_hash,fee,reward,operation,openfield FROM transactions "
                "WHERE block_height > 0 ORDER BY block_height ASC, rowid ASC")
    rows = cur.fetchall()
    conn.close()
    verifiable = max(0, len({r[H] for r in rows}) - 1)
    ok = True
    for label, mode in (("as-stored", False), ("integer-roundtrip", True)):
        m = verify_blocks(rows, integer_roundtrip=mode)
        print(f"{label}: {len(m)} mismatch(es) over {verifiable} verifiable blocks")
        for x in m[:10]:
            print("  ", x)
        ok = ok and not m
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main()
