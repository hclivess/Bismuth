"""
Write-path crash-safety tests for the SQLite stores (db-hardening).

These lock in the two PREVENTION hardenings that protect the node against an unclean shutdown
(OOM / SIGKILL / power loss) splitting its separate ledger.db / hyper.db / index.db / mempool.db
files mid-write:

  (1) WAL + crash-safe `synchronous` on EVERY store the node opens. Proven by building a real
      DbHandler (and a real Mempool) against throwaway DB files and reading `PRAGMA journal_mode`
      back — it must say 'wal'. WAL is what turns a kill mid-write into a clean rollback to the last
      committed transaction instead of a torn page.

  (2) The crash-recovery contract: ledger.db is the single source of truth for the on-disk tip, and a
      torn (uncommitted) write reopens at the LAST COMMITTED block, never corrupt and never partially
      applied. Proven by simulating the kill directly: commit block N, then open a transaction that
      writes block N+1 and DISCARD the connection without committing (the OS would do this on SIGKILL),
      then reopen the file and assert the tip is still N and the row count is intact.

WHY THIS IS A UNIT/LOGIC TEST, NOT A LIVE-NODE TEST (honest limits):
We deliberately do NOT boot a node here. The hardening under test is pure connection-setup +
commit-ordering durability, which is fully exercised by constructing the real DbHandler/Mempool
objects against temp files. This keeps the test single-process and cheap (the host has OOM'd under
heavy multi-process load) and never touches the live node's static/*.db. It proves WAL/synchronous are
set and that an uncommitted txn rolls back; it does not prove a real kernel SIGKILL (that is the OS's
job, which WAL is specifically designed to make safe).

Run with: python3 -m pytest tests/test_db_crash_safety.py -v
"""
import os
import sqlite3
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import dbhandler  # noqa: E402


# Minimal ledger schema (transactions + misc), matching regnet.SQL_LEDGER's column shapes. Only the
# structure matters for durability/ordering — not the genesis contents.
_CREATE_MISC = "CREATE TABLE misc (block_height INTEGER, difficulty TEXT)"
_CREATE_TX = (
    "CREATE TABLE transactions (block_height INTEGER, timestamp NUMERIC, address TEXT, "
    "recipient TEXT, amount NUMERIC, signature TEXT, public_key TEXT, block_hash TEXT, "
    "fee NUMERIC, reward NUMERIC, operation TEXT, openfield TEXT)"
)
# index.db carries a different schema (aliases/tokens); the durability test only needs it to be a valid
# SQLite file the handler can open, so an empty schema is enough.


class _AppLog:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _Logger:
    def __init__(self):
        self.app_log = _AppLog()


def _make_ledger_db(path):
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_MISC)
    conn.execute(_CREATE_TX)
    conn.commit()
    conn.close()


def _make_index_db(path):
    # an empty but valid SQLite file is all the durability/pragma check needs
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE aliases (block_height INTEGER, address TEXT, alias TEXT)")
    conn.commit()
    conn.close()


def _insert_block(cursor, height):
    """Insert one coinbase-shaped reward row + its misc/difficulty row for `height`."""
    cursor.execute(
        "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (height, 1493640955.47 + height, "genesis", "addr", "1", "sig", "pub",
         f"hash{height}", "0", "1", "0", "data"),
    )
    cursor.execute("INSERT INTO misc VALUES (?, ?)", (height, "1000"))


@pytest.fixture
def db_paths(tmp_path):
    ledger = str(tmp_path / "ledger.db")
    hyper = str(tmp_path / "hyper.db")
    index = str(tmp_path / "index.db")
    _make_ledger_db(ledger)
    _make_ledger_db(hyper)
    _make_index_db(index)
    return types.SimpleNamespace(ledger=ledger, hyper=hyper, index=index, tmp=tmp_path)


def _new_handler(db_paths, ram=False):
    return dbhandler.DbHandler(
        index_db=db_paths.index,
        ledger_path=db_paths.ledger,
        hyper_path=db_paths.hyper,
        ram=ram,
        ledger_ram_file="file:ledger_crashtest?mode=memory&cache=shared",
        logger=_Logger(),
        trace_db_calls=False,
    )


# ---------------------------------------------------------------------------------------------------
# (a) Every store the DbHandler opens enters WAL mode, with the documented synchronous level.
# ---------------------------------------------------------------------------------------------------

def _journal_mode(conn):
    return conn.execute("PRAGMA journal_mode;").fetchone()[0].lower()


def _synchronous(conn):
    # 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
    return conn.execute("PRAGMA synchronous;").fetchone()[0]


def test_all_dbhandler_stores_open_in_wal_mode(db_paths):
    h = _new_handler(db_paths, ram=False)
    try:
        # index.db, ledger.db, hyper.db are real on-disk files -> must be WAL.
        assert _journal_mode(h.index) == "wal", "index.db not in WAL"
        assert _journal_mode(h.hdd) == "wal", "ledger.db not in WAL"
        assert _journal_mode(h.hdd2) == "wal", "hyper.db not in WAL"
        # the working connection is hyper.db when ram=False -> also WAL.
        assert _journal_mode(h.conn) == "wal", "working store not in WAL"
    finally:
        h.close()


def test_ledger_is_synchronous_full_others_normal(db_paths):
    # The authoritative ledger (ledger.db / self.hdd) is the single source of truth, so it gets the
    # strongest durability (FULL=2 -> survives a power cut). The re-derivable stores get NORMAL=1.
    h = _new_handler(db_paths, ram=False)
    try:
        assert _synchronous(h.hdd) == 2, "ledger.db must be synchronous=FULL (2)"
        assert _synchronous(h.index) == 1, "index.db should be synchronous=NORMAL (1)"
        assert _synchronous(h.hdd2) == 1, "hyper.db should be synchronous=NORMAL (1)"
    finally:
        h.close()


def test_wal_mode_survives_reopen(db_paths):
    # journal_mode=WAL is a persistent property of the DB file, so a fresh independent connection (the
    # post-restart node) must still see WAL even after the handler that set it is gone.
    h = _new_handler(db_paths, ram=False)
    h.close()
    for path in (db_paths.ledger, db_paths.hyper):
        conn = sqlite3.connect(path)
        try:
            assert _journal_mode(conn) == "wal", f"{path} lost WAL after reopen"
        finally:
            conn.close()


# ---------------------------------------------------------------------------------------------------
# (b) A torn (uncommitted) write reopens at the LAST COMMITTED block — not corrupt, not half-applied.
# ---------------------------------------------------------------------------------------------------

def test_torn_write_rolls_back_to_last_committed_block(db_paths):
    """Commit block 1, then start a transaction writing block 2 and 'crash' (discard the connection
    without committing). Reopen the ledger: it must be back at block 1, intact."""
    h = _new_handler(db_paths, ram=False)
    # Commit a clean block 1 to ledger.db.
    _insert_block(h.h, 1)
    h.commit(h.hdd)
    assert h.block_height_max() == 1

    # Begin an explicit transaction, stage block 2, but NEVER commit. Simulate SIGKILL by dropping the
    # connection objects (no .commit, no graceful .close of the writer).
    h.hdd.execute("BEGIN")
    _insert_block(h.h, 2)
    # rows are visible to THIS uncommitted connection...
    assert h.h.execute("SELECT max(block_height) FROM transactions").fetchone()[0] == 2
    # ...now the process "dies": abandon the handler without committing the open txn.
    del h

    # A fresh handler (the restarted node) opens the same files.
    h2 = _new_handler(db_paths, ram=False)
    try:
        # The uncommitted block 2 was rolled back: ledger.db is back at the last committed tip (1).
        assert h2.block_height_max() == 1, "torn write was not rolled back to the last committed block"
        # And exactly one transaction row + one misc row survive — no partial/torn state.
        assert h2.h.execute("SELECT count(*) FROM transactions").fetchone()[0] == 1
        assert h2.h.execute("SELECT count(*) FROM misc").fetchone()[0] == 1
        # The file is fully usable (not corrupt): a normal integrity check passes.
        assert h2.hdd.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
    finally:
        h2.close()


def test_committed_block_persists_across_reopen(db_paths):
    # The flip side of the torn-write test: a COMMITTED block must survive the reopen. Proves the
    # rollback above isn't just throwing away all writes.
    h = _new_handler(db_paths, ram=False)
    _insert_block(h.h, 1)
    _insert_block(h.h, 2)
    h.commit(h.hdd)
    h.close()

    h2 = _new_handler(db_paths, ram=False)
    try:
        assert h2.block_height_max() == 2
        assert h2.h.execute("SELECT count(*) FROM transactions").fetchone()[0] == 2
    finally:
        h2.close()


def test_ledger_is_the_authoritative_tip_source(db_paths):
    """block_height_max() (the value node.hdd_block is derived from) must read ledger.db (self.hdd),
    not hyper.db — so ledger.db is the single source of truth for the committed tip. We put DIFFERENT
    tips in the two files and assert block_height_max() follows ledger.db."""
    h = _new_handler(db_paths, ram=False)
    try:
        # ledger.db (self.hdd) gets up to height 5; hyper.db (self.hdd2) gets one block more (height 6),
        # mimicking the live per-block order (hyper committed first, ledger last).
        for ht in (1, 2, 3, 4, 5):
            _insert_block(h.h, ht)
        h.commit(h.hdd)
        for ht in (1, 2, 3, 4, 5, 6):
            _insert_block(h.h2, ht)
        h.commit(h.hdd2)

        # The authoritative tip the runtime trusts is ledger.db's (5), NOT hyper.db's (6).
        assert h.block_height_max() == 5, "block_height_max must reflect ledger.db, the source of truth"
        assert h.block_height_max_hyper() == 6, "sanity: hyper.db really is ahead in this fixture"
    finally:
        h.close()


# ---------------------------------------------------------------------------------------------------
# (a, mempool) The on-disk mempool also opens in WAL with synchronous=NORMAL.
# ---------------------------------------------------------------------------------------------------

def test_disk_mempool_opens_in_wal(tmp_path):
    import mempool as mp

    config = types.SimpleNamespace(
        mempool_ram=False,
        version="mainnet",
        mempool_path=str(tmp_path / "mempool.db"),
    )
    pool = mp.Mempool(_AppLog(), config=config, db_lock=None, testnet=False, trace_db_calls=False)
    try:
        assert _journal_mode(pool.db) == "wal", "on-disk mempool not in WAL"
        assert _synchronous(pool.db) == 1, "on-disk mempool should be synchronous=NORMAL (1)"
    finally:
        pool.close()
