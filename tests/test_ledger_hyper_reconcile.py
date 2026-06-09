"""
Regression test for the ledger.db-vs-hyper.db startup reconciliation (chain_ops.reconcile_ledger_hyper).

Root cause it guards: Bismuth writes the full ledger (ledger.db) and the hyperblock rollup (hyper.db)
through SEPARATE connections at different moments per block, with no atomic cross-DB transaction. An
unclean exit (OOM SIGKILL / power loss) can leave one file several blocks AHEAD of the other. The
runtime then derives node.hdd_block from ledger.db but reads its working tip / last hash from hyper.db;
when they disagree the first post-restart digest rejects the next block ("... already in ledger"),
node.hdd_block is snapped down to the shorter store, and the node WEDGES re-fetching a height it
already half-holds. This is exactly the live-mainnet wedge (ledger at 4847528, hyper at 4847513).

These tests build tiny on-disk ledger.db + hyper.db fixtures and drive the REAL reconciliation, so they
need no running node. They assert it heals deterministically to a single consistent tip BEFORE sync.
"""
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import chain_ops


class _Log:
    def __init__(self):
        self.messages = []

    def warning(self, *a, **k):
        self.messages.append(a[0] if a else "")

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _Logger:
    def __init__(self):
        self.app_log = _Log()


class _Node:
    trace_db_calls = False

    def __init__(self, ledger_path, hyper_path):
        self.ledger_path = ledger_path
        self.hyper_path = hyper_path
        self.logger = _Logger()


def _make_chain(path, top, *, tx_top=None, misc_top=None):
    """Create a 12-col transactions + misc chain with one coinbase row (reward != 0) per height
    1..top. tx_top / misc_top let a caller make ONE table end higher than the other (to model a
    crash mid-write). Returns nothing; the file is closed."""
    tx_top = top if tx_top is None else tx_top
    misc_top = top if misc_top is None else misc_top
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute(
        "CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, amount, "
        "signature, public_key, block_hash, fee, reward, operation, openfield)")
    c.execute("CREATE TABLE misc (block_height INTEGER, difficulty)")
    for h in range(1, tx_top + 1):
        c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (h, "0", "miner", "miner", "0", "sig%d" % h, "0", "hash%d" % h, "0", "1", "0", "0"))
    for h in range(1, misc_top + 1):
        c.execute("INSERT INTO misc VALUES (?, ?)", (h, "1.0"))
    conn.commit()
    conn.close()


def _tips(path):
    conn = sqlite3.connect(path)
    try:
        tx = conn.execute("SELECT max(block_height) FROM transactions").fetchone()[0]
        misc = conn.execute("SELECT max(block_height) FROM misc").fetchone()[0]
        return tx, misc
    finally:
        conn.close()


def test_reconcile_rolls_ledger_back_to_hyper(tmp_path):
    """The live wedge: full ledger 15 blocks AHEAD of the hyperblock rollup. Reconciliation must roll
    ledger.db back to hyper.db's tip so both stores share ONE contiguous height, and must leave the
    common bulk (<= the hyper tip) completely intact so the node can then advance from there."""
    ledger = str(tmp_path / "ledger.db")
    hyper = str(tmp_path / "hyper.db")
    _make_chain(ledger, 4847528)   # ahead
    _make_chain(hyper, 4847513)    # the runtime tip the node actually serves from

    node = _Node(ledger, hyper)
    chain_ops.reconcile_ledger_hyper(node)

    assert _tips(ledger) == (4847513, 4847513), "ledger.db should be trimmed to the common height"
    assert _tips(hyper) == (4847513, 4847513), "hyper.db (already at the floor) must be untouched"

    # The agreed bulk must NOT have been touched: every height up to the common tip survives, and the
    # next height the node will re-download (4847514) is genuinely gone from BOTH stores -> no collision.
    conn = sqlite3.connect(ledger)
    try:
        assert conn.execute("SELECT count(*) FROM transactions WHERE block_height=4847513").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM transactions WHERE block_height=4847514").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM transactions WHERE block_height=4000000").fetchone()[0] == 1
    finally:
        conn.close()


def test_reconcile_rolls_hyper_back_to_ledger(tmp_path):
    """Symmetric case: hyper.db ended up ahead of ledger.db. The common height is the ledger tip, so
    hyper.db is the store that gets trimmed. Both must converge to the same single tip."""
    ledger = str(tmp_path / "ledger.db")
    hyper = str(tmp_path / "hyper.db")
    _make_chain(ledger, 1000)
    _make_chain(hyper, 1007)

    node = _Node(ledger, hyper)
    chain_ops.reconcile_ledger_hyper(node)

    assert _tips(ledger) == (1000, 1000)
    assert _tips(hyper) == (1000, 1000)


def test_reconcile_handles_split_within_one_store(tmp_path):
    """A crash can also leave transactions and misc at different heights WITHIN one file (to_db inserts
    misc then transactions in one txn, but a partial WAL checkpoint across files can still desync the
    four tips). The common height is the min of all four, and every table in both files ends there."""
    ledger = str(tmp_path / "ledger.db")
    hyper = str(tmp_path / "hyper.db")
    # ledger: tx=520, misc=519 ; hyper: tx=505, misc=505  -> common = 505
    _make_chain(ledger, 520, tx_top=520, misc_top=519)
    _make_chain(hyper, 505)

    node = _Node(ledger, hyper)
    chain_ops.reconcile_ledger_hyper(node)

    assert _tips(ledger) == (505, 505)
    assert _tips(hyper) == (505, 505)


def test_reconcile_noop_when_already_consistent(tmp_path):
    """When the two stores already agree, reconciliation must be a silent no-op (no trimming, no log
    noise) — it runs on every single startup, so the healthy path must be cheap and quiet."""
    ledger = str(tmp_path / "ledger.db")
    hyper = str(tmp_path / "hyper.db")
    _make_chain(ledger, 2000)
    _make_chain(hyper, 2000)

    node = _Node(ledger, hyper)
    chain_ops.reconcile_ledger_hyper(node)

    assert _tips(ledger) == (2000, 2000)
    assert _tips(hyper) == (2000, 2000)
    # No "mismatch"/"trimmed"/"reconciled" lines on the happy path.
    joined = " ".join(node.logger.app_log.messages).lower()
    assert "mismatch" not in joined and "trimmed" not in joined and "reconcil" not in joined


def test_reconcile_noop_in_hyperblock_mode_same_path(tmp_path):
    """In full_ledger=False mode ledger_path == hyper_path (ledger is a clone of the hyperblocks);
    there is nothing to cross-check and the function must return immediately without touching the
    file or raising."""
    shared = str(tmp_path / "regmode.db")
    _make_chain(shared, 300)

    node = _Node(shared, shared)
    chain_ops.reconcile_ledger_hyper(node)  # must not raise

    assert _tips(shared) == (300, 300)


def test_reconcile_then_node_can_advance(tmp_path):
    """End-to-end intent: after reconciliation the node's runtime tip logic (block_height_max from
    ledger.db, last_block_hash from hyper.db) agrees on ONE height, so the next block (common+1) is
    free to be digested into both stores with no "already in ledger" collision. We simulate that
    advance by inserting common+1 into both files and confirming the chain stays contiguous."""
    ledger = str(tmp_path / "ledger.db")
    hyper = str(tmp_path / "hyper.db")
    _make_chain(ledger, 4847528)
    _make_chain(hyper, 4847513)

    node = _Node(ledger, hyper)
    chain_ops.reconcile_ledger_hyper(node)

    common = 4847513
    for path in (ledger, hyper):
        conn = sqlite3.connect(path)
        # the new block's height must be free (the wedge was that it was NOT, on the ledger side)
        assert conn.execute("SELECT count(*) FROM transactions WHERE block_height=?",
                             (common + 1,)).fetchone()[0] == 0
        conn.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (common + 1, "0", "miner", "miner", "0", "newsig", "0", "newhash", "0", "1", "0", "0"))
        conn.execute("INSERT INTO misc VALUES (?, ?)", (common + 1, "1.0"))
        conn.commit()
        conn.close()

    assert _tips(ledger) == (common + 1, common + 1)
    assert _tips(hyper) == (common + 1, common + 1)
