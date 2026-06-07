"""
Maintained balance index tests (balance_index.BalanceIndex).

Proves the running per-address credit/debit index is byte-identical to the authoritative
`ledger_balance3` aggregate (in integer units): a synthetic integer ledger (varied coinbase / sends /
self-sends), standalone apply+rollback, and — on the live regnet node (which runs in integer-amount
mode) — that the index rebuilt from the ledger bit-matches the SQL aggregate for EVERY address.

Run with: python3 -m pytest tests/test_balance_index.py -v
"""
import sqlite3
from time import sleep

import pytest

pytest.importorskip("lmdb")

from balance_index import BalanceIndex

BIRECIP = "0badf00d" * 7   # valid 56-hex addresses, distinct from other tests
BIRECIP2 = "feedface" * 7

# a 12-field row with INTEGER-unit amount/fee/reward
def _row(h, addr, recip, amount, fee=0, reward=0):
    return [h, "%.2f" % (1600000000 + h), addr, recip, amount, "sig%d" % h, "pub", "h%d" % h,
            fee, reward, "", ""]


def _make_int_ledger(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, "
                 "amount INTEGER, signature, public_key, block_hash, fee INTEGER, reward INTEGER, "
                 "operation, openfield)")
    rows = [
        _row(1, "miner", "miner", 0, reward=500000000),          # coinbase: miner +5 BIS
        _row(2, "miner", "alice", 100000000, fee=1000000),       # miner -> alice 1 BIS, fee 0.01
        _row(2, "miner", "miner", 0, reward=500000000),          # block 2 coinbase
        _row(3, "alice", "bob", 30000000, fee=1000000),          # alice -> bob 0.3
        _row(4, "bob", "bob", 5000000, fee=1000000),             # bob self-send (fee burns net)
        _row(5, "alice", "alice", 0, fee=1000000),               # alice fee-only
    ]
    conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _sql_balance_units(conn, addr):
    c = conn.execute("SELECT COALESCE(SUM(amount+reward),0) FROM transactions WHERE recipient=?",
                     (addr,)).fetchone()[0]
    d = conn.execute("SELECT COALESCE(SUM(amount+fee),0) FROM transactions WHERE address=?",
                     (addr,)).fetchone()[0]
    return int(c) - int(d)


def _all_addresses(conn):
    return {a for (a,) in conn.execute("SELECT DISTINCT address FROM transactions")} | \
           {a for (a,) in conn.execute("SELECT DISTINCT recipient FROM transactions")}


def test_rebuild_bitmatches_sql_synthetic(tmp_path):
    ledger = str(tmp_path / "ledger.db")
    _make_int_ledger(ledger)
    idx = BalanceIndex(str(tmp_path / "bi"), map_size=64 * 1024 * 1024)
    try:
        assert idx.rebuild_from_ledger(ledger) >= 3
        conn = sqlite3.connect(ledger)
        try:
            for a in _all_addresses(conn):
                assert idx.get_balance_units(a) == _sql_balance_units(conn, a), \
                    "index != ledger_balance3 for %s" % a
        finally:
            conn.close()
        # spot-check the known balances
        assert idx.get_balance_units("bob") == 30000000 - 1000000        # received 0.3, paid 0.01 fee
        assert idx.get_balance_units("alice") == 100000000 - 30000000 - 1000000 - 1000000
    finally:
        idx.close()


def test_apply_then_rollback_is_neutral(tmp_path):
    idx = BalanceIndex(str(tmp_path / "bi"), map_size=64 * 1024 * 1024)
    try:
        block = [_row(7, "A", "B", 100, fee=10), _row(7, "C", "A", 40, fee=5)]
        idx.apply_rows(block)
        assert idx.get_balance_units("B") == 100
        assert idx.get_balance_units("A") == 40 - (100 + 10)   # received 40, sent 100 + fee 10
        assert idx.get_balance_units("C") == -(40 + 5)
        idx.rollback_rows(block)                                # reorg: undo
        for a in ("A", "B", "C"):
            assert idx.get_balance_units(a) == 0
    finally:
        idx.close()


def test_bitmatches_ledger_on_regnet(client, tmp_path):
    # create varied real balances on the live regnet node, then mine to clear the mempool
    client.mine(3)
    client.send(BIRECIP, 1.5)
    client.send(BIRECIP2, 0.75)
    client.mine(3)
    sleep(0.6)
    idx = BalanceIndex(str(tmp_path / "bi"), map_size=128 * 1024 * 1024)
    try:
        assert idx.rebuild_from_ledger("static/regmode.db") > 0
        conn = sqlite3.connect("file:static/regmode.db?mode=ro", uri=True, timeout=20)
        conn.text_factory = str
        try:
            for a in _all_addresses(conn):                      # EVERY address must bit-match
                iu, su = idx.get_balance_units(a), _sql_balance_units(conn, a)
                if iu != su:
                    rows = conn.execute(
                        "SELECT block_height, amount, typeof(amount), fee, typeof(fee), reward, "
                        "typeof(reward) FROM transactions WHERE address=? OR recipient=? LIMIT 6",
                        (a, a)).fetchall()
                    pytest.fail("addr=%s index=%s sql=%s rows=%s" % (a, iu, su, rows))
        finally:
            conn.close()
    finally:
        idx.close()
