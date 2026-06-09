"""
Lockstep-commit tests: ledger.db and hyper.db must advance their committed tip TOGETHER, and a torn
write must leave them at the SAME last-committed height (never persistently split).

Background (see dbhandler_write's CRASH-RECOVERY CONTRACT and dbhandler's lockstep machinery):
ledger.db (authoritative) and hyper.db (working) are written by separate connections/transactions, so
historically a crash between to_db (hyper.db) and db_to_drive (ledger.db) could split them by up to a
whole sync batch. The fix stamps a single-row `commit_marker` ("the height at which BOTH files agree")
into each file and advances BOTH markers in ONE transaction at db_to_drive, via a dedicated commit
connection that ATTACHes hyper.db onto ledger.db.

WAL is not atomic across attached databases (sqlite.org/wal.html: "atomic for each individual database,
but not atomic across all databases as a set"), so this is HONESTLY a near-atomic improvement: a crash
mid-finalize can leave the two markers AT MOST ONE in-flight block apart, and because they only ever
advance together, the startup reconcile (chain_ops.reconcile_ledger_hyper) always heals down to the
lower marker — a single, well-defined height. These tests prove exactly those two properties:

  (1) After EACH committed block, ledger.db and hyper.db share the same tip AND the same marker.
  (2) A simulated torn write (db_to_drive's atomic transaction discarded without commit) leaves BOTH
      files at the SAME last-committed marker — they roll back together, never split.
  (3) The ledger-rows + both-markers write is ONE transaction (proven by aborting it and seeing the
      ledger rows roll back in lockstep with the markers).
  (4) reconcile_ledger_hyper uses the markers as the authoritative floor and converges a marker-split.

Single-process, no live node, throwaway temp DBs — never touches static/*.db.

Run with: python3 -m pytest tests/test_lockstep_commit.py -v
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
import chain_ops  # noqa: E402


_CREATE_MISC = "CREATE TABLE misc (block_height INTEGER, difficulty TEXT)"
_CREATE_TX = (
    "CREATE TABLE transactions (block_height INTEGER, timestamp NUMERIC, address TEXT, "
    "recipient TEXT, amount NUMERIC, signature TEXT, public_key TEXT, block_hash TEXT, "
    "fee NUMERIC, reward NUMERIC, operation TEXT, openfield TEXT)"
)


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
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE aliases (block_height INTEGER, address TEXT, alias TEXT)")
    conn.commit()
    conn.close()


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
        ledger_ram_file="file:ledger_lockstep?mode=memory&cache=shared",
        logger=_Logger(),
        trace_db_calls=False,
    )


def _node(ledger, hyper):
    """The minimal node object db_to_drive touches: hdd_block (the floor we flush from), last_block /
    last_block_hash (the new tip), ram=False (live full-ledger path), is_regnet=False."""
    return types.SimpleNamespace(
        is_regnet=False, ram=False, hdd_block=0, hdd_hash="genesis",
        last_block=0, last_block_hash="genesis",
        ledger_path=ledger, hyper_path=hyper, trace_db_calls=False,
        logger=_Logger())


def _stage_block_in_working_store(h, height):
    """Mimic to_db: write one coinbase-shaped tx row + its misc row into the WORKING store (hyper.db
    via self.c) and commit it — exactly what happens per block BEFORE db_to_drive runs."""
    h.execute_param(h.c, h.SQL_TO_TRANSACTIONS,
                    (height, "0", "miner", "miner", "1", f"sig{height}", "0",
                     f"hash{height}", "0", "1", "0", "0"))
    h.execute_param(h.c, "INSERT INTO misc VALUES (?, ?)", (height, "1.0"))
    h.commit(h.conn)


def _tip(path, table="transactions"):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT max(block_height) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _marker(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT committed_height FROM commit_marker WHERE id = 0").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------------
# Setup wiring: the lockstep machinery is actually engaged for the live (distinct-file) path.
# ---------------------------------------------------------------------------------------------------

def test_lockstep_engaged_and_marker_tables_created(db_paths):
    h = _new_handler(db_paths, ram=False)
    try:
        assert h._separate_ledger_hyper is True, "distinct ledger/hyper files must engage lockstep"
        assert h.commit_conn is not None, "the ATTACH commit connection must be open"
        # Both files carry the (seeded, NULL) marker row.
        assert _marker(db_paths.ledger) is None
        assert _marker(db_paths.hyper) is None
        # The commit connection really has hyper.db attached as `hyperdb`.
        dbs = [r[1] for r in h.commit_conn.execute("PRAGMA database_list").fetchall()]
        assert "hyperdb" in dbs, "hyper.db should be ATTACHed as hyperdb on the commit connection"
    finally:
        h.close()


# ---------------------------------------------------------------------------------------------------
# (1) After each committed block, ledger.db and hyper.db share the SAME tip AND the SAME marker.
# ---------------------------------------------------------------------------------------------------

def test_each_block_keeps_ledger_and_hyper_in_step(db_paths):
    h = _new_handler(db_paths, ram=False)
    node = _node(db_paths.ledger, db_paths.hyper)
    try:
        for height in range(1, 6):
            # to_db: stage+commit the block in the working store (hyper.db) FIRST.
            _stage_block_in_working_store(h, height)
            node.last_block = height
            node.last_block_hash = f"hash{height}"
            # db_to_drive: atomic flush to ledger.db + advance BOTH markers in one txn.
            h.db_to_drive(node)

            # After the flush, BOTH files agree on the tip AND the marker == this height.
            assert _tip(db_paths.ledger) == height, f"ledger tip should be {height}"
            assert _tip(db_paths.hyper) == height, f"hyper tip should be {height}"
            assert _marker(db_paths.ledger) == height, f"ledger marker should be {height}"
            assert _marker(db_paths.hyper) == height, f"hyper marker should be {height}"
            # misc tips too (the whole block, not just tx rows).
            assert _tip(db_paths.ledger, "misc") == height
            assert _tip(db_paths.hyper, "misc") == height
            # node floor advanced to the committed tip.
            assert node.hdd_block == height
    finally:
        h.close()


def test_marker_is_height_where_both_agree_between_to_db_and_db_to_drive(db_paths):
    """The marker means 'the height at which both files agree'. Stage block 1 in the working store
    (hyper rows -> 1) but DON'T flush yet: hyper's MARKER must still be the pre-block value (None),
    because the marker only advances when ledger.db catches up in db_to_drive. This is what bounds a
    crash to a single in-flight block."""
    h = _new_handler(db_paths, ram=False)
    node = _node(db_paths.ledger, db_paths.hyper)
    try:
        # one clean block so both markers are at 1
        _stage_block_in_working_store(h, 1)
        node.last_block, node.last_block_hash = 1, "hash1"
        h.db_to_drive(node)
        assert _marker(db_paths.ledger) == 1 and _marker(db_paths.hyper) == 1

        # stage block 2 in hyper working store, but do NOT call db_to_drive (the in-flight window)
        _stage_block_in_working_store(h, 2)
        assert _tip(db_paths.hyper) == 2, "hyper ROWS ran ahead (to_db committed block 2)"
        assert _marker(db_paths.hyper) == 1, "hyper MARKER stays at the agreed height until db_to_drive"
        assert _marker(db_paths.ledger) == 1
        assert _tip(db_paths.ledger) == 1, "ledger ROWS still at the agreed height"
    finally:
        h.close()


# ---------------------------------------------------------------------------------------------------
# (2)+(3) A torn db_to_drive (atomic txn discarded) leaves BOTH files at the SAME last-committed marker.
# ---------------------------------------------------------------------------------------------------

def test_torn_db_to_drive_leaves_both_at_same_committed_height(db_paths):
    """Commit blocks 1..3 cleanly (both files + markers at 3). Then SIMULATE a crash during the next
    flush: open the commit connection's atomic transaction writing block 4's ledger rows + both markers,
    and DISCARD it without committing (a SIGKILL). Reopen: both files must be back at marker 3 and tip 3
    — rolled back TOGETHER, never split."""
    h = _new_handler(db_paths, ram=False)
    node = _node(db_paths.ledger, db_paths.hyper)

    for height in (1, 2, 3):
        _stage_block_in_working_store(h, height)
        node.last_block, node.last_block_hash = height, f"hash{height}"
        h.db_to_drive(node)
    assert _marker(db_paths.ledger) == 3 and _marker(db_paths.hyper) == 3

    # The hyper working store races ahead to block 4 (to_db committed it) — this is the in-flight block.
    _stage_block_in_working_store(h, 4)

    # Now begin the db_to_drive atomic txn for block 4 on the commit connection, but never commit it.
    cc = h.commit_conn
    cc.execute("BEGIN IMMEDIATE")
    cc.execute(h.SQL_TO_TRANSACTIONS,
               (4, "0", "miner", "miner", "1", "sig4", "0", "hash4", "0", "1", "0", "0"))
    cc.execute("UPDATE main.commit_marker SET committed_height = 4 WHERE id = 0")
    cc.execute("UPDATE hyperdb.commit_marker SET committed_height = 4 WHERE id = 0")
    # rows/markers visible to THIS uncommitted connection...
    assert cc.execute("SELECT committed_height FROM main.commit_marker").fetchone()[0] == 4
    # ...the process "dies": drop the handler without committing the open txn.
    del h
    del cc

    # Restart: a fresh handler opens the same files.
    h2 = _new_handler(db_paths, ram=False)
    try:
        # The torn flush rolled back: BOTH markers are back at 3, and they MATCH (no split).
        assert _marker(db_paths.ledger) == 3, "ledger marker must roll back to 3"
        assert _marker(db_paths.hyper) == 3, "hyper marker must roll back to 3"
        assert _marker(db_paths.ledger) == _marker(db_paths.hyper), "markers must agree — never split"
        # ledger ROWS rolled back with their marker (single-file atomic): block 4 is gone from ledger.
        assert _tip(db_paths.ledger) == 3, "ledger rows must roll back to 3 in lockstep with its marker"
        # hyper ROWS are at 4 (to_db's commit survived) but its MARKER is 3 -> recovery floor is 3.
        assert _tip(db_paths.hyper) == 4, "hyper working rows survived at 4 (the in-flight block)"
        # The split is exactly ONE block and the markers pin the unambiguous floor (3).
        assert _tip(db_paths.hyper) - _marker(db_paths.hyper) == 1, "gap is provably the single in-flight block"
        assert h2.hdd.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        assert h2.hdd2.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
    finally:
        h2.close()


def test_ledger_rows_and_markers_are_one_transaction(db_paths):
    """Prove the ledger rows + both markers are a SINGLE transaction: abort it and confirm the ledger
    rows roll back together with the markers (not left half-applied)."""
    h = _new_handler(db_paths, ram=False)
    node = _node(db_paths.ledger, db_paths.hyper)
    try:
        _stage_block_in_working_store(h, 1)
        node.last_block, node.last_block_hash = 1, "hash1"
        h.db_to_drive(node)  # clean block 1

        cc = h.commit_conn
        cc.execute("BEGIN IMMEDIATE")
        cc.execute(h.SQL_TO_TRANSACTIONS,
                   (2, "0", "miner", "miner", "1", "sig2", "0", "hash2", "0", "1", "0", "0"))
        cc.execute("UPDATE main.commit_marker SET committed_height = 2 WHERE id = 0")
        cc.execute("UPDATE hyperdb.commit_marker SET committed_height = 2 WHERE id = 0")
        cc.rollback()  # abort the whole unit

        # Nothing from the aborted txn survived: ledger rows AND markers are still at 1.
        assert _tip(db_paths.ledger) == 1, "aborted ledger rows must not persist"
        assert _marker(db_paths.ledger) == 1, "aborted ledger marker must not persist"
        assert _marker(db_paths.hyper) == 1, "aborted hyper marker must not persist"
    finally:
        h.close()


# ---------------------------------------------------------------------------------------------------
# (4) reconcile_ledger_hyper uses the markers as the authoritative floor.
# ---------------------------------------------------------------------------------------------------

def test_reconcile_uses_markers_to_heal_a_split(db_paths):
    """Build the exact post-crash state the lockstep design produces: ledger rows+marker at 3, hyper
    rows at 4 but marker at 3 (the in-flight block). reconcile_ledger_hyper must read the markers, pick
    the floor 3, and trim hyper's stray row 4 so both files end at a single consistent tip 3."""
    # ledger: tx=3, misc=3, marker=3
    conn = sqlite3.connect(db_paths.ledger)
    conn.execute("CREATE TABLE IF NOT EXISTS commit_marker (id INTEGER PRIMARY KEY CHECK(id=0), committed_height INTEGER)")
    for hgt in (1, 2, 3):
        conn.execute(_INS_TX := "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (hgt, "0", "m", "m", "1", f"s{hgt}", "0", f"h{hgt}", "0", "1", "0", "0"))
        conn.execute("INSERT INTO misc VALUES (?, ?)", (hgt, "1.0"))
    conn.execute("INSERT INTO commit_marker VALUES (0, 3)")
    conn.commit()
    conn.close()
    # hyper: tx=4, misc=4 (rows ran ahead), marker=3 (the agreed floor)
    conn = sqlite3.connect(db_paths.hyper)
    conn.execute("CREATE TABLE IF NOT EXISTS commit_marker (id INTEGER PRIMARY KEY CHECK(id=0), committed_height INTEGER)")
    for hgt in (1, 2, 3, 4):
        conn.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (hgt, "0", "m", "m", "1", f"s{hgt}", "0", f"h{hgt}", "0", "1", "0", "0"))
        conn.execute("INSERT INTO misc VALUES (?, ?)", (hgt, "1.0"))
    conn.execute("INSERT INTO commit_marker VALUES (0, 3)")
    conn.commit()
    conn.close()

    node = _node(db_paths.ledger, db_paths.hyper)
    chain_ops.reconcile_ledger_hyper(node)

    # Both converge to the marker floor (3); the in-flight hyper row 4 is trimmed.
    assert _tip(db_paths.ledger) == 3 and _tip(db_paths.hyper) == 3
    assert _tip(db_paths.ledger, "misc") == 3 and _tip(db_paths.hyper, "misc") == 3
    # Markers are re-stamped to the reconciled height so the files now fully agree.
    assert _marker(db_paths.ledger) == 3 and _marker(db_paths.hyper) == 3


def test_reconcile_marker_floor_below_tip_min(db_paths):
    """The markers are AUTHORITATIVE for direction even when the bare tip-min would pick a higher
    height. ledger marker=2 though its rows reach 3 (e.g. a marker that lagged a flush): the floor must
    follow the marker (2), not max-tip heuristics, and both files end at 2."""
    for path, marker_val in ((db_paths.ledger, 2), (db_paths.hyper, 2)):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE IF NOT EXISTS commit_marker (id INTEGER PRIMARY KEY CHECK(id=0), committed_height INTEGER)")
        for hgt in (1, 2, 3):
            conn.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (hgt, "0", "m", "m", "1", f"s{hgt}", "0", f"h{hgt}", "0", "1", "0", "0"))
            conn.execute("INSERT INTO misc VALUES (?, ?)", (hgt, "1.0"))
        conn.execute("INSERT INTO commit_marker VALUES (0, ?)", (marker_val,))
        conn.commit()
        conn.close()

    node = _node(db_paths.ledger, db_paths.hyper)
    chain_ops.reconcile_ledger_hyper(node)

    assert _tip(db_paths.ledger) == 2 and _tip(db_paths.hyper) == 2, "floor must follow the markers (2)"


def test_reconcile_falls_back_to_tips_when_markers_absent(db_paths):
    """Legacy DBs (no commit_marker table at all) must still reconcile via the original tip-min path —
    backward compatibility. ledger=5, hyper=7, no markers -> both trim to 5."""
    # Build legacy files WITHOUT a commit_marker table.
    for path, top in ((db_paths.ledger, 5), (db_paths.hyper, 7)):
        conn = sqlite3.connect(path)
        for hgt in range(1, top + 1):
            conn.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (hgt, "0", "m", "m", "1", f"s{hgt}", "0", f"h{hgt}", "0", "1", "0", "0"))
            conn.execute("INSERT INTO misc VALUES (?, ?)", (hgt, "1.0"))
        conn.commit()
        conn.close()
    assert chain_ops._marker_of(db_paths.ledger) is None  # truly legacy

    node = _node(db_paths.ledger, db_paths.hyper)
    chain_ops.reconcile_ledger_hyper(node)

    assert _tip(db_paths.ledger) == 5 and _tip(db_paths.hyper) == 5


# ---------------------------------------------------------------------------------------------------
# Single-file path (regnet / hyperblock mode): no ATTACH, no split possible.
# ---------------------------------------------------------------------------------------------------

def test_single_file_path_does_not_open_attach_connection(tmp_path):
    """When ledger_path == hyper_path there is one file; lockstep must be disabled (no ATTACH-self) and
    db_to_drive falls back to the legacy commit. The handler must still construct cleanly."""
    shared = str(tmp_path / "regmode.db")
    index = str(tmp_path / "index.db")
    _make_ledger_db(shared)
    _make_index_db(index)
    h = dbhandler.DbHandler(
        index_db=index, ledger_path=shared, hyper_path=shared, ram=False,
        ledger_ram_file="file:ledger_single?mode=memory&cache=shared",
        logger=_Logger(), trace_db_calls=False)
    try:
        assert h._separate_ledger_hyper is False, "same path must NOT engage cross-file lockstep"
        assert h.commit_conn is None, "no ATTACH connection for a single file"
    finally:
        h.close()


def test_reconcile_clamps_stale_high_markers_by_real_tips(db_paths):
    """Defence in depth: a rollback (or any path that trimmed rows but not the marker) can leave the
    markers STALE-HIGH. reconcile must never trust a marker above the rows that actually exist — it
    clamps the floor by the real tips. ledger+hyper rows at 3 but BOTH markers say 9 -> floor is 3."""
    for path in (db_paths.ledger, db_paths.hyper):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE IF NOT EXISTS commit_marker (id INTEGER PRIMARY KEY CHECK(id=0), committed_height INTEGER)")
        for hgt in (1, 2, 3):
            conn.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (hgt, "0", "m", "m", "1", f"s{hgt}", "0", f"h{hgt}", "0", "1", "0", "0"))
            conn.execute("INSERT INTO misc VALUES (?, ?)", (hgt, "1.0"))
        conn.execute("INSERT INTO commit_marker VALUES (0, 9)")  # stale-high marker
        conn.commit()
        conn.close()

    node = _node(db_paths.ledger, db_paths.hyper)
    # Tips already agree (3==3==3==3) but markers (9) disagree with the tip -> NOT the silent fast path;
    # reconcile must run, clamp to the real tip 3, and re-stamp the markers down to 3.
    chain_ops.reconcile_ledger_hyper(node)

    assert _tip(db_paths.ledger) == 3 and _tip(db_paths.hyper) == 3
    assert _marker(db_paths.ledger) == 3 and _marker(db_paths.hyper) == 3, "stale markers must be re-stamped to the real tip"


def test_rollback_under_keeps_markers_at_new_tip(db_paths):
    """rollback_under (reorg/blocknf path) must leave the markers at the post-rollback committed tip,
    so the 'marker == committed tip' invariant holds without waiting for the next db_to_drive."""
    h = _new_handler(db_paths, ram=False)
    node = _node(db_paths.ledger, db_paths.hyper)
    try:
        for height in (1, 2, 3, 4, 5):
            _stage_block_in_working_store(h, height)
            node.last_block, node.last_block_hash = height, f"hash{height}"
            h.db_to_drive(node)
        assert _marker(db_paths.ledger) == 5 and _marker(db_paths.hyper) == 5

        # Roll back everything from height 4 up -> new tip 3 on both ledger.db and hyper.db.
        h.rollback_under(4)

        assert h.read_committed_height(h.hdd) == 3, "ledger marker must follow the rollback to 3"
        assert h.read_committed_height(h.hdd2) == 3, "hyper marker must follow the rollback to 3"
        # And the markers match the actual row tips.
        assert _tip(db_paths.ledger) == 3 and _tip(db_paths.hyper) == 3
    finally:
        h.close()
