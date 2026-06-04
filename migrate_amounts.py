"""
Offline migration of a Bismuth ledger to **explicit column types** with **integer atomic-unit**
amounts (doc/16 phase 2). This is the first concrete bite of the storage column migration.

It is intentionally an OFFLINE, deliberate tool — not an auto-startup migration — because rebuilding
the consensus ledger table must be done on purpose and replay-verified. The recommended cutover is:

    python3 replay_verify.py legacy_ledger.db          # baseline: expect 0 mismatches
    python3 migrate_amounts.py legacy_ledger.db new_ledger.db
    python3 migrate_amounts.py --verify new_ledger.db  # expect 0 mismatches (integer-unit columns)

`amount` / `fee` / `reward` become INTEGER atomic units (1 BIS = 100_000_000); `timestamp` and the
text fields become explicit TEXT (lossless — fixing the legacy NUMERIC-affinity coercion). Consensus
bytes stay frozen in bismuth_serialize: the `'%.8f'` / `'%.2f'` strings are reconstructed from the
integer/text columns only at the consensus / legacy-API edge, which `replay_verify` proves preserves
every block hash.
"""
import sqlite3

import amounts
import replay_verify

TRANSACTIONS_SCHEMA = (
    "CREATE TABLE transactions ("
    "block_height INTEGER, timestamp TEXT, address TEXT, recipient TEXT, amount INTEGER, "
    "signature TEXT, public_key TEXT, block_hash TEXT, fee INTEGER, reward INTEGER, "
    "operation TEXT, openfield TEXT)"
)
MISC_SCHEMA = "CREATE TABLE misc (block_height INTEGER, difficulty TEXT)"

_SELECT = ("SELECT block_height,timestamp,address,recipient,amount,signature,public_key,block_hash,"
           "fee,reward,operation,openfield FROM transactions")


def migrate(src_conn, dst_conn):
    """Copy `src_conn`'s ledger into `dst_conn` with explicit types and integer-unit amounts."""
    sc, dc = src_conn.cursor(), dst_conn.cursor()
    dc.execute(TRANSACTIONS_SCHEMA)
    dc.execute(MISC_SCHEMA)
    sc.execute(_SELECT)
    for r in sc.fetchall():
        dc.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            int(r[0]),                    # block_height
            "%.2f" % float(r[1]),         # timestamp -> canonical TEXT
            str(r[2]), str(r[3]),         # address, recipient
            amounts.to_units(r[4]),       # amount -> integer atomic units
            str(r[5]), str(r[6]), str(r[7]),  # signature, public_key, block_hash
            amounts.to_units(r[8]),       # fee -> integer atomic units
            amounts.to_units(r[9]),       # reward -> integer atomic units
            str(r[10]), str(r[11]),       # operation, openfield
        ))
    sc.execute("SELECT block_height, difficulty FROM misc")
    for r in sc.fetchall():
        dc.execute("INSERT INTO misc VALUES (?,?)", (int(r[0]), str(r[1])))
    dc.execute("PRAGMA user_version = 2")
    dst_conn.commit()


def verify_integer_ledger(conn):
    """Recompute every block hash from an already-migrated (integer-unit) ledger. [] == all good."""
    cur = conn.cursor()
    cur.execute(_SELECT + " WHERE block_height > 0 ORDER BY block_height ASC, rowid ASC")
    return replay_verify.verify_blocks(cur.fetchall(), amounts_are_units=True)


def _main():
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--verify" in sys.argv:
        conn = sqlite3.connect(args[0])
        m = verify_integer_ledger(conn)
        conn.close()
        print("integer-unit ledger: {} mismatch(es)".format(len(m)))
        for x in m[:10]:
            print("  ", x)
        sys.exit(0 if not m else 1)
    if len(args) < 2:
        print("usage: migrate_amounts.py <src.db> <dst.db>   |   migrate_amounts.py --verify <db>")
        sys.exit(2)
    src, dst = sqlite3.connect(args[0]), sqlite3.connect(args[1])
    migrate(src, dst)
    mismatches = verify_integer_ledger(dst)
    src.close()
    dst.close()
    print("migrated {} -> {}; {} block-hash mismatch(es)".format(args[0], args[1], len(mismatches)))
    sys.exit(0 if not mismatches else 1)


if __name__ == "__main__":
    _main()
