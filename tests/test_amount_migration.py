"""Offline integer-amount column migration: hash-preservation, integer storage, and balance correctness."""
# Offline integer-amount column migration: proves the migration preserves every block hash, actually
# stores integer atomic units with explicit types, and that integer-summed balances are correct.
# Self-contained (no node). See doc/16 phase 2. Run with: python3 -m pytest -v

import sqlite3

import amounts
import bismuth_serialize
import migrate_amounts

ADDR = "a" * 56
OTHER = "b" * 56
PK = "public-key-b64"

LEGACY_SCHEMA = (
    "CREATE TABLE transactions (block_height INTEGER, timestamp TEXT, address TEXT, recipient TEXT, "
    "amount TEXT, signature TEXT, public_key TEXT, block_hash TEXT, fee TEXT, reward TEXT, "
    "operation TEXT, openfield TEXT)")


def _build_legacy():
    """A small legacy-format ledger with correctly chained block hashes (amounts as decimal text)."""
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute(LEGACY_SCHEMA)
    cur.execute("CREATE TABLE misc (block_height INTEGER, difficulty TEXT)")
    state = {"prev": "0" * 56}

    def add(height, txs):  # txs: (recipient, amount, fee, reward, operation, openfield, signature)
        tuples, rows = [], []
        for recip, amt, fee, rew, op, of, sig in txs:
            ts = "%.2f" % (1500000000.0 + height)
            tuples.append((ts, ADDR, recip, amt, sig, PK, op, of))      # exactly what gets hashed
            rows.append([height, ts, ADDR, recip, amt, sig, PK, None, fee, rew, op, of])
        block_hash = bismuth_serialize.block_hash(tuples, state["prev"])
        for row in rows:
            row[7] = block_hash
            cur.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
        cur.execute("INSERT INTO misc VALUES (?,?)", (height, "50.0000000000"))
        state["prev"] = block_hash

    add(1, [(ADDR, "0.00000000", "0.00000000", "10.00000000", "0", "nonce1", "s1")])  # anchor
    add(2, [(ADDR, "0.00000000", "0.00000000", "10.00000000", "0", "nonce2", "s2")])
    add(3, [(OTHER, "1.50000000", "0.01050000", "0.00000000", "test", "data", "s3a"),
            (ADDR, "0.00000000", "0.00000000", "10.01050000", "0", "nonce3", "s3b")])  # send + coinbase
    add(4, [(ADDR, "0.00000000", "0.00000000", "10.00000000", "0", "nonce4", "s4")])
    conn.commit()
    return conn


def _migrated():
    dst = sqlite3.connect(":memory:")
    migrate_amounts.migrate(_build_legacy(), dst)
    return dst


def test_migration_preserves_block_hashes():
    assert migrate_amounts.verify_integer_ledger(_migrated()) == []


def test_migration_stores_integer_units_with_explicit_types():
    db = _migrated()
    cur = db.cursor()
    cur.execute("SELECT amount FROM transactions WHERE recipient = ?", (OTHER,))
    assert cur.fetchone()[0] == 150_000_000              # 1.5 BIS as atomic units
    cur.execute("SELECT type FROM pragma_table_info('transactions') WHERE name = 'amount'")
    assert cur.fetchone()[0] == "INTEGER"
    cur.execute("SELECT type FROM pragma_table_info('transactions') WHERE name = 'timestamp'")
    assert cur.fetchone()[0] == "TEXT"


def test_integer_summed_balance_matches_decimal():
    db = _migrated()
    cur = db.cursor()
    cur.execute("SELECT COALESCE(SUM(amount + reward), 0) FROM transactions WHERE recipient = ?", (OTHER,))
    credit = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(amount + fee), 0) FROM transactions WHERE address = ?", (OTHER,))
    debit = cur.fetchone()[0]
    assert amounts.from_units(credit - debit) == "1.50000000"   # OTHER received 1.5, spent nothing
