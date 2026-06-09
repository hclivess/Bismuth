"""
chain_ops.sequencing_check must not leak its per-chain DB connection on an in-loop exception (L6).

The function opens a connection per chain (node.ledger_path / node.hyper_path) and only closed it at the
bottom of the loop; a SQL error mid-loop propagated WITHOUT closing it, leaking the handle (which keeps
the ledger file locked, notably on Windows). The fix wraps the per-chain loop body in try/finally.

This test makes the in-loop `misc` query raise (the table is absent) — so only the per-chain connection
is open when the error fires — and asserts that connection is still closed despite the exception.

Run with: python3 -m pytest tests/test_sequencing_conn_leak.py -v
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
    def warning(self, *a, **k):
        pass
    info = warning


class _Logger:
    app_log = _Log()


class _Node:
    trace_db_calls = False
    is_mainnet = False

    def __init__(self, ledger_path, hyper_path):
        self.ledger_path = ledger_path
        self.hyper_path = hyper_path
        self.logger = _Logger()


class _NoopDb:
    def tokens_rollback(self, *a):
        pass

    def aliases_rollback(self, *a):
        pass

    def execute_param(self, conn, sql, params):
        conn.execute(sql, params)


class _TrackedConn:
    """A thin proxy over a real sqlite3 connection that records whether close() was called.
    (sqlite3.Connection.close is read-only, so we can't patch the method on the instance.)"""
    def __init__(self, conn, rec):
        self._conn = conn
        self._rec = rec

    def close(self):
        self._rec["closed"] = True
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _tracking_connect(monkeypatch):
    """Patch chain_ops.sqlite3.connect to record every connection and whether it was closed."""
    opened = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        rec = {"closed": False}
        opened.append(rec)
        return _TrackedConn(real_connect(*args, **kwargs), rec)

    monkeypatch.setattr(chain_ops.sqlite3, "connect", tracking_connect)
    return opened


def test_connection_closed_when_in_loop_query_raises(tmp_path, monkeypatch):
    # transactions table is well-sequenced (no rollback branch), but the `misc` table is MISSING, so the
    # second in-loop query raises OperationalError with only the per-chain connection open.
    ledger = str(tmp_path / "ledger.db")
    hyper = str(tmp_path / "hyper.db")
    for p in (ledger, hyper):
        conn = sqlite3.connect(p)
        conn.execute("CREATE TABLE transactions (block_height INTEGER, reward INTEGER)")
        conn.execute("INSERT INTO transactions VALUES (2, 1)")    # single reward row -> clean sequencing
        conn.commit()
        conn.close()                                              # NOTE: no `misc` table created

    opened = _tracking_connect(monkeypatch)
    monkeypatch.chdir(tmp_path)
    node = _Node(ledger, hyper)

    with pytest.raises(sqlite3.OperationalError):
        chain_ops.sequencing_check(node, _NoopDb())

    assert opened, "expected sequencing_check to open a connection"
    leaked = [r for r in opened if not r["closed"]]
    assert not leaked, f"{len(leaked)} per-chain connection(s) leaked on the exception path"


def test_clean_run_closes_connections(tmp_path, monkeypatch):
    """A well-sequenced chain completes and still closes every connection (the normal path)."""
    ledger = str(tmp_path / "ledger.db")
    hyper = str(tmp_path / "hyper.db")
    for p in (ledger, hyper):
        conn = sqlite3.connect(p)
        conn.execute("CREATE TABLE transactions (block_height INTEGER, reward INTEGER)")
        conn.execute("CREATE TABLE misc (block_height INTEGER, difficulty TEXT)")
        conn.execute("INSERT INTO transactions VALUES (2, 1)")    # single reward row -> no gap
        conn.commit()
        conn.close()

    opened = _tracking_connect(monkeypatch)
    monkeypatch.chdir(tmp_path)                                   # keep sequencing_last out of the cwd
    node = _Node(ledger, hyper)

    chain_ops.sequencing_check(node, _NoopDb())
    assert opened and all(r["closed"] for r in opened)
