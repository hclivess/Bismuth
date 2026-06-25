"""
Maintained txid -> height index tests (txid_index.TxidIndex, doc/26 stage 4).

Proves the LMDB projection that backs the post-fork duplicate-signature replay read:
  * only POST-FORK positive-height rows are indexed (pre-fork txs keep signature dedup; reward mirrors skip);
  * the stored key is the SAME canonical content txid the digester computes (txid_of == bismuth_serialize.tx_id_at);
  * height_of / contains(max_height) bound the replay check to the confirmed chain;
  * rebuild_from_cursor and apply/rollback agree, and a reorg drops txids above the new tip.

Run with: python3 -m pytest tests/test_txid_index.py -v
"""
import sqlite3

import pytest

pytest.importorskip("lmdb")

import amounts
import bismuth_serialize
from txid_index import TxidIndex, txid_of

FORK = 100  # synthetic fork activation height


@pytest.fixture(autouse=True)
def _legacy_amount_mode():
    # These hermetic rows use legacy decimal-string amounts, so pin LEDGER_INTEGER False regardless of
    # suite order (another test may leave the global flag flipped to integer-unit mode).
    saved = amounts.LEDGER_INTEGER
    amounts.LEDGER_INTEGER = False
    try:
        yield
    finally:
        amounts.LEDGER_INTEGER = saved


# a 12-field ledger row with a legacy decimal-string amount (LEDGER_INTEGER stays False here)
def _row(h, addr, recip, amount="0.00000000", op="", of="", fee="0.00000000", reward="0.00000000"):
    return [h, "%.2f" % (1700000000 + abs(h)), addr, recip, amount,
            "sig%d_%s" % (h, recip), "", "h%d" % h, fee, reward, op, of]


A = "aa" * 28  # valid-length 56-hex-ish placeholders (the codec only needs strings)
B = "bb" * 28
C = "cc" * 28


def _expected(row):
    return bismuth_serialize.tx_id_at(int(row[0]), FORK, str(row[1]), str(row[2]), str(row[3]),
                                      "%.8f" % float(row[4]), str(row[10]), str(row[11]))


def test_apply_and_bounded_contains(tmp_path):
    idx = TxidIndex(str(tmp_path / "txi"), map_size=64 * 1024 * 1024)
    try:
        r1 = _row(FORK + 1, A, B, "1.50000000")
        r2 = _row(FORK + 5, B, C, "0.25000000", op="msg", of="hi")
        assert idx.apply_rows([r1, r2], FORK) == 2

        # the index key is exactly the canonical content txid the digester derives
        t1, t2 = txid_of(r1, FORK), txid_of(r2, FORK)
        assert t1 == _expected(r1) and t2 == _expected(r2)

        assert idx.height_of(t1) == FORK + 1
        assert idx.height_of(t2) == FORK + 5
        assert idx.height_of("deadbeef") is None

        # contains bounds the replay check to [0, confirmed_tip]
        assert idx.contains(t2) is True
        assert idx.contains(t2, max_height=FORK + 5) is True
        assert idx.contains(t2, max_height=FORK + 4) is False   # confirmed tip below it -> not yet seen
    finally:
        idx.close()


def test_prefork_and_mirror_rows_skipped(tmp_path):
    idx = TxidIndex(str(tmp_path / "txi"), map_size=64 * 1024 * 1024)
    try:
        pre = _row(FORK - 1, A, B, "1.00000000")     # pre-fork: signature-deduped, not indexed
        mirror = _row(-(FORK + 2), A, A, "0.00000000", reward="5.00000000")  # negative-height reward mirror
        post = _row(FORK + 2, A, C, "2.00000000")
        assert idx.apply_rows([pre, mirror, post], FORK) == 1
        assert idx.count() == 1
        assert idx.height_of(txid_of(post, FORK)) == FORK + 2
    finally:
        idx.close()


def test_fork_none_is_inert(tmp_path):
    idx = TxidIndex(str(tmp_path / "txi"), map_size=64 * 1024 * 1024)
    try:
        assert idx.apply_rows([_row(FORK + 1, A, B, "1.0")], None) == 0
        assert idx.count() == 0
    finally:
        idx.close()


def _make_ledger(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, "
                 "amount, signature, public_key, block_hash, fee, reward, operation, openfield)")
    rows = [
        _row(FORK - 2, A, B, "1.00000000"),                 # pre-fork  -> skipped
        _row(FORK, A, B, "3.00000000"),                      # at fork   -> indexed
        _row(FORK + 1, B, C, "0.50000000", op="data"),       # post-fork -> indexed
        _row(-(FORK + 1), A, A, reward="5.00000000"),        # mirror    -> skipped (negative height)
        _row(FORK + 3, C, A, "0.10000000"),                  # post-fork -> indexed
    ]
    conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_rebuild_from_cursor(tmp_path):
    conn = _make_ledger(str(tmp_path / "ledger.db"))
    idx = TxidIndex(str(tmp_path / "txi"), map_size=64 * 1024 * 1024)
    try:
        assert idx.rebuild_from_cursor(conn.cursor(), FORK) == 3   # 3 post-fork positive-height rows
        assert idx.count() == 3
        assert idx.height_of(txid_of(_row(FORK, A, B, "3.00000000"), FORK)) == FORK
        # rebuild is idempotent (drops + re-indexes)
        assert idx.rebuild_from_cursor(conn.cursor(), FORK) == 3
        assert idx.count() == 3
        # fork None empties it
        assert idx.rebuild_from_cursor(conn.cursor(), None) == 0
        assert idx.count() == 0
    finally:
        idx.close()
        conn.close()


@pytest.mark.parametrize("backend", ["lmdb", "sqlite-kv"])
def test_backend_swappable(tmp_path, backend):
    """The SAME TxidIndex runs identically on a 2nd engine through open_store(backend=...) — proving the KV
    seam (incl. the new drop via rebuild, apply, rollback's iterate) is engine-independent for this store.
    Both backends must produce the identical canonical txid->height results. [doc/26 storage stage 1]"""
    conn = _make_ledger(str(tmp_path / "ledger.db"))
    idx = TxidIndex(str(tmp_path / ("txi_" + backend)), map_size=64 * 1024 * 1024, backend=backend)
    try:
        assert idx.rebuild_from_cursor(conn.cursor(), FORK) == 3   # drop + reindex -> FORK, FORK+1, FORK+3
        assert idx.count() == 3
        assert idx.height_of(txid_of(_row(FORK, A, B, "3.00000000"), FORK)) == FORK
        r = _row(FORK + 9, A, C, "7.00000000")
        assert idx.apply_rows([r], FORK) == 1
        tr = txid_of(r, FORK)
        assert idx.contains(tr) and idx.contains(tr, max_height=FORK + 9)
        assert not idx.contains(tr, max_height=FORK + 8)
        assert idx.rollback(FORK + 2) == 2                          # drops FORK+3 and FORK+9
        assert idx.count() == 2
        assert idx.height_of(tr) is None
    finally:
        idx.close()
        conn.close()


def test_rollback_drops_above_tip(tmp_path):
    idx = TxidIndex(str(tmp_path / "txi"), map_size=64 * 1024 * 1024)
    try:
        rows = [_row(FORK + h, A, B, "%d.00000000" % h) for h in (1, 2, 3, 4)]
        idx.apply_rows(rows, FORK)
        assert idx.count() == 4
        # reorg back to tip FORK+2: drop FORK+3, FORK+4
        assert idx.rollback(FORK + 2) == 2
        assert idx.count() == 2
        assert idx.contains(txid_of(rows[1], FORK)) is True    # FORK+2 kept
        assert idx.contains(txid_of(rows[2], FORK)) is False   # FORK+3 dropped
    finally:
        idx.close()
