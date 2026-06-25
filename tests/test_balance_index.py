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


# --------------------------------------------------------------------------- KV-seam migration (doc/26 stage 1)
def test_lmdb_on_disk_bytes_identical_to_direct_lmdb(tmp_path):
    """hf2 Stage-4 (doc/40): the BalanceIndex value is now two fixed u128 LITTLE-endian counters
    (credit_units, debit_units) — TRUE bytes, not a msgpack list. This re-baselines the on-disk-bytes
    characterization lock to the new format (a deliberate, fork-gated format break). The address key is
    unchanged (raw address bytes, C5)."""
    lmdb = pytest.importorskip("lmdb")

    def _be(c, d):  # the new raw value: two u128 little-endian
        return c.to_bytes(16, "little") + d.to_bytes(16, "little")

    idx = BalanceIndex(str(tmp_path / "bi"), map_size=64 * 1024 * 1024)
    try:
        # B credited 100; A credited 40 debited 110; C debited 45
        idx.apply_rows([_row(7, "A", "B", 100, fee=10), _row(7, "C", "A", 40, fee=5)])
    finally:
        idx.close()

    env = lmdb.open(str(tmp_path / "bi"), subdir=True, max_dbs=1, readonly=True, lock=False)
    db = env.open_db(b"bal")
    on_disk = {}
    with env.begin() as t:
        for k, v in t.cursor(db=db):
            on_disk[bytes(k)] = bytes(v)
    env.close()

    expected = {
        b"A": _be(40, 100 + 10),   # credited 40, debited amount 100 + fee 10
        b"B": _be(100, 0),
        b"C": _be(0, 40 + 5),
    }
    assert on_disk == expected
    # every value is the fixed 32-byte true-bytes form, never msgpack
    assert all(len(v) == 32 for v in on_disk.values())


def _drive_balance_index(backend, tmp_path, name):
    """Identical workload against a BalanceIndex on the given backend; return observable results."""
    ledger = str(tmp_path / (name + "_ledger.db"))
    _make_int_ledger(ledger)
    idx = BalanceIndex(str(tmp_path / name), map_size=64 * 1024 * 1024, backend=backend)
    try:
        rebuilt = idx.rebuild_from_ledger(ledger)
        snap = {a: idx.get_balance_units(a) for a in ("miner", "alice", "bob")}
        count_after_rebuild = idx.count()
        # apply a fresh block, then roll it back -> net neutral
        block = [_row(7, "alice", "bob", 100, fee=10), _row(7, "miner", "alice", 40, fee=5)]
        idx.apply_rows(block)
        applied = {a: idx.get_balance_units(a) for a in ("miner", "alice", "bob")}
        idx.rollback_rows(block)
        rolled = {a: idx.get_balance_units(a) for a in ("miner", "alice", "bob")}
        return {
            "rebuilt": rebuilt,
            "snap": snap,
            "count": count_after_rebuild,
            "applied": applied,
            "rolled": rolled,
        }
    finally:
        idx.close()


def test_backend_swappable_lmdb_equals_sqlite(tmp_path):
    """The SAME BalanceIndex, driven identically on lmdb vs sqlite-kv via open_store(backend=...), yields
    identical results — proving the KV seam (get/put, drop via rebuild, stat via count) is engine-
    independent for this funds-sensitive store. [doc/26 storage stage 1]"""
    pytest.importorskip("lmdb")
    res_lmdb = _drive_balance_index("lmdb", tmp_path, "bi_lmdb")
    res_sqlite = _drive_balance_index("sqlite-kv", tmp_path, "bi_sqlite")
    assert res_lmdb == res_sqlite, \
        "BalanceIndex results differ across backends -> seam not engine-independent"
    # sanity: the workload is non-trivial and rollback is neutral
    assert res_lmdb["rebuilt"] >= 3
    assert res_lmdb["snap"] == res_lmdb["rolled"]
    assert res_lmdb["applied"] != res_lmdb["snap"]


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
