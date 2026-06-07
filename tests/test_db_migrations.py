# Unit tests for the schema-migration framework (pure; no node needed).
# Run with: python3 -m pytest -v

import sqlite3

import db_migrations


def _fresh_ledger():
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE transactions (block_height, signature, address, recipient)")
    cur.execute("CREATE TABLE misc (block_height, difficulty)")
    return conn, cur


def _indexes(cur):
    cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
    return {r[0] for r in cur.fetchall()}


def test_migration_sets_version_creates_indexes_and_is_idempotent():
    conn, cur = _fresh_ledger()
    migs = db_migrations.ledger_migrations(old_sqlite=False)
    assert db_migrations.run(cur, migs) == 2
    idx = _indexes(cur)
    assert "TXID4_Index" in idx
    assert "Misc Block Height Index" in idx
    assert "Address Height Index" in idx          # v2 composite indexes (fast address history)
    assert "Recipient Height Index" in idx
    # running again is a no-op and the version stays put
    assert db_migrations.run(cur, migs) == 2
    assert _indexes(cur) == idx


def test_old_sqlite_skips_the_substr_index():
    conn, cur = _fresh_ledger()
    assert db_migrations.run(cur, db_migrations.ledger_migrations(old_sqlite=True)) == 2
    idx = _indexes(cur)
    assert "TXID4_Index" not in idx
    assert "Misc Block Height Index" in idx
    assert "Address Height Index" in idx          # v2 still runs under old_sqlite
