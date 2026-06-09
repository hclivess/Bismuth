"""
Regression tests for the storage / db-handling bug-fix pass.

Each test pins ONE concrete, non-consensus bug that was found by auditing the storage modules
(dbhandler*, mempool*, block_store, balance_index, chain_ops, …) and proves the fix. They are
deliberately self-contained: they build tiny on-disk SQLite/LMDB fixtures and drive the real
functions, so they do NOT need a running regnet node.
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import chain_ops


# ---------------------------------------------------------------------------------------------------
# Minimal fakes for chain_ops.sequencing_check (it only needs these attributes).
# ---------------------------------------------------------------------------------------------------
class _Log:
    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _Logger:
    app_log = _Log()


class _DbHandler:
    """tokens_rollback / aliases_rollback are the only methods the transaction-sequencing-error
    branch calls; the real ones just log + swallow, so no-ops are faithful."""

    def tokens_rollback(self, node, height):
        pass

    def aliases_rollback(self, node, height):
        pass

    def execute_param(self, cursor, query, param):
        cursor.execute(query, param)


class _Node:
    trace_db_calls = False
    logger = _Logger()

    def __init__(self, ledger_path, hyper_path):
        self.ledger_path = ledger_path
        self.hyper_path = hyper_path


def _make_ledger_with_gap(path):
    """A 12-column transactions table whose coinbase (reward != 0) heights jump 2,3 -> 5 (4 missing),
    which is exactly the 'transaction sequencing error' sequencing_check looks for."""
    conn = sqlite3.connect(path)
    c = conn.cursor()
    c.execute(
        "CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, amount, "
        "signature, public_key, block_hash, fee, reward, operation, openfield)")
    c.execute("CREATE TABLE misc (block_height INTEGER, difficulty)")
    for h in (2, 3, 5):  # gap at 4
        c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (h, "0", "miner", "miner", "0", "0", "0", "hash%d" % h, "0", "1", "0", "0"))
    conn.commit()
    conn.close()


def test_sequencing_check_does_not_leak_sqlite_connections(tmp_path, monkeypatch):
    """chain_ops.sequencing_check opened a fresh sqlite3 connection (`conn2`) inside the
    transaction-sequencing-error branch but never closed it (the structurally identical misc-error
    branch does). On a real node this leaks an OS file handle + an open DB connection every time the
    recovery path fires. The fix mirrors the misc branch's close(); here we wrap sqlite3.connect to
    assert every connection opened during the call is closed when it returns."""
    ledger = str(tmp_path / "ledger.db")
    hyper = str(tmp_path / "hyper.db")
    _make_ledger_with_gap(ledger)
    _make_ledger_with_gap(hyper)

    # Run from tmp_path so the 'sequencing_last' anchor file it writes/reads doesn't touch the repo.
    monkeypatch.chdir(tmp_path)

    opened = []  # list of _TrackedConn

    real_connect = sqlite3.connect

    class _TrackedConn:
        """Thin proxy: delegates everything to a real sqlite3 connection but records close().
        (sqlite3.Connection.close is read-only, so we can't patch the method on the instance.)"""

        def __init__(self, conn):
            self._conn = conn
            self.closed = False

        def close(self):
            self.closed = True
            return self._conn.close()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def tracking_connect(*a, **k):
        tc = _TrackedConn(real_connect(*a, **k))
        opened.append(tc)
        return tc

    monkeypatch.setattr(chain_ops.sqlite3, "connect", tracking_connect)

    node = _Node(ledger, hyper)
    chain_ops.sequencing_check(node, _DbHandler())

    leaked = [tc for tc in opened if not tc.closed]
    # Defensively close anything left open so the leak can't poison other tests / tmp cleanup.
    for tc in leaked:
        try:
            tc.close()
        except Exception:
            pass

    assert opened, "sequencing_check should have opened at least one sqlite connection"
    assert not leaked, (
        "sequencing_check leaked %d sqlite connection(s) (the transaction-sequencing-error branch "
        "never closed its conn2)" % len(leaked))
