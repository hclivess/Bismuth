"""Equivalence test for the optimized hyperblock-rollup balance computation
(`chain_ops._rollup_balances`).

The old `recompress_ledger` collapsed pruned history by looping over every distinct
recipient and running two indexed scans per address — O(addresses x scans), hours on a
deep rollup. The optimized version does one range scan with in-memory accumulation. This
test proves the two produce BYTE-IDENTICAL collapsed balances on varied synthetic data,
because the hyperblock balances feed pruned-node balance reads and must not change.

Run: python3 -m pytest tests/test_recompress_rollup.py -v
"""
import os
import sqlite3
import sys
from decimal import Decimal

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import chain_ops
from quantizer import quantize_eight

SCHEMA = ("CREATE TABLE transactions (block_height INTEGER, timestamp NUMERIC, address TEXT, "
          "recipient TEXT, amount NUMERIC, signature TEXT, public_key TEXT, block_hash TEXT, "
          "fee NUMERIC, reward NUMERIC, operation TEXT, openfield TEXT)")


def _old_rollup(conn, depth_specific):
    """Faithful reimplementation of the ORIGINAL per-recipient double-scan rollup."""
    cur = conn.cursor()
    cur.execute("SELECT distinct(recipient) FROM transactions WHERE (block_height < ? AND block_height > ?) "
                "ORDER BY block_height;", (depth_specific, -depth_specific))
    uniq = cur.fetchall()
    out = {}
    for x in set(uniq):
        credit = Decimal("0")
        for entry in conn.execute("SELECT amount,reward FROM transactions WHERE recipient = ? AND "
                                  "(block_height < ? AND block_height > ?);",
                                  (x[0], depth_specific, -depth_specific)):
            credit = quantize_eight(credit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
        debit = Decimal("0")
        for entry in conn.execute("SELECT amount,fee FROM transactions WHERE address = ? AND "
                                  "(block_height < ? AND block_height > ?);",
                                  (x[0], depth_specific, -depth_specific)):
            debit = quantize_eight(debit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
        end_balance = quantize_eight(credit - debit)
        if end_balance > 0:
            out[x[0]] = end_balance
    return out


def _mk(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(SCHEMA)
    for r in rows:
        conn.execute("INSERT INTO transactions (block_height,timestamp,address,recipient,amount,"
                     "signature,public_key,block_hash,fee,reward,operation,openfield) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (r["h"], 1600000000 + abs(r["h"]), r["addr"], r["rcpt"], r["amount"],
                      "sig", "pk", "bh", r.get("fee", "0"), r.get("reward", "0"), "0", ""))
    conn.commit()
    return conn


def _assert_equiv(rows, depth_specific):
    conn = _mk(rows)
    old = _old_rollup(conn, depth_specific)
    new = chain_ops._rollup_balances(conn, depth_specific)
    conn.close()
    # compare as {addr: str(balance)} so Decimal exponent quirks don't mask a real diff
    assert {k: str(v) for k, v in old.items()} == {k: str(v) for k, v in new.items()}, \
        "rollup mismatch\nold=%s\nnew=%s" % (old, new)
    return new


def test_basic_credit_debit():
    rows = [
        {"h": 2, "addr": "miner", "rcpt": "A", "amount": "0", "reward": "10.0"},     # A receives 10
        {"h": 3, "addr": "A", "rcpt": "B", "amount": "4.0", "fee": "0.01"},          # A sends 4 (+0.01 fee), B +4
        {"h": 4, "addr": "miner", "rcpt": "C", "amount": "0", "reward": "10.0"},     # C +10
        {"h": 5, "addr": "C", "rcpt": "A", "amount": "2.5", "fee": "0.01"},          # C -2.51, A +2.5
    ]
    out = _assert_equiv(rows, depth_specific=100)
    # spot-check A: +10 -4 -0.01 +2.5 = 8.49
    assert out["A"] == quantize_eight(Decimal("8.49"))


def test_high_precision_amounts_like_mirror_rows():
    # mirror reward rows carry >8-decimal amounts; per-entry quantize_eight must match in both methods
    rows = [
        {"h": 6, "addr": "Development Reward", "rcpt": "G", "amount": "2.83637272727273"},
        {"h": 7, "addr": "Development Reward", "rcpt": "G", "amount": "2.83638181818182"},
        {"h": 8, "addr": "Hypernode Payouts", "rcpt": "H", "amount": "14.23335"},
    ]
    _assert_equiv(rows, depth_specific=100)


def test_only_sender_excluded_and_negative_excluded():
    rows = [
        {"h": 2, "addr": "miner", "rcpt": "X", "amount": "0", "reward": "5.0"},   # X +5
        {"h": 3, "addr": "X", "rcpt": "Y", "amount": "5.0", "fee": "0.5"},        # X -5.5 -> net -0.5 (excluded, <0)
        # Z only ever SENDS in range (never a recipient) -> not in distinct(recipient) -> no row
        {"h": 4, "addr": "Z", "rcpt": "Y", "amount": "1.0"},
    ]
    out = _assert_equiv(rows, depth_specific=100)
    assert "X" not in out          # net negative -> excluded
    assert "Z" not in out          # only-sender -> excluded (matches original)
    assert out.get("Y") == quantize_eight(Decimal("6.0"))


def test_range_boundary_and_mirror_rows():
    rows = [
        {"h": 2, "addr": "miner", "rcpt": "A", "amount": "0", "reward": "10.0"},   # in range
        {"h": 150, "addr": "miner", "rcpt": "A", "amount": "0", "reward": "99.0"}, # OUT (kept tail, > depth_specific)
        {"h": -10, "addr": "Development Reward", "rcpt": "A", "amount": "1.0"},    # mirror, in range (> -depth_specific)
        {"h": -150, "addr": "Development Reward", "rcpt": "A", "amount": "7.0"},   # mirror, OUT (< -depth_specific)
    ]
    out = _assert_equiv(rows, depth_specific=100)
    # A: in-range credits only = 10 (h2) + 1 (h-10) = 11; the h150 and h-150 rows are excluded
    assert out["A"] == quantize_eight(Decimal("11.0"))


def test_many_addresses_random_equivalence():
    import random
    rng = random.Random(1234)
    addrs = ["addr%02d" % i for i in range(25)]
    rows = []
    for h in range(2, 400):
        a = rng.choice(addrs); r = rng.choice(addrs)
        rows.append({"h": h, "addr": a, "rcpt": r,
                     "amount": "%.8f" % rng.uniform(0, 50),
                     "fee": "%.8f" % rng.uniform(0, 0.1),
                     "reward": "%.8f" % (rng.uniform(0, 12) if a == "addr00" else 0)})
    _assert_equiv(rows, depth_specific=300)


# ---------------------------------------------------------------------------
# On-disk swap hygiene: the "hangs at Recompressing" wedge after a non-clean stop.
#
# recompress_ledger() rebuilds hyper.db by writing a `.temp` clone and swapping it in. The swap used
# to remove ONLY hyper.db, so a stale `hyper.db-wal`/`-shm` left by a non-clean exit (impatient
# Ctrl-C / hard kill — the graceful flag is only honoured by the main loop, not during startup)
# survived and got mis-associated with the freshly-renamed hyper.db on the next open, wedging SQLite
# WAL recovery ("disk image is malformed" / indefinite stall). The only cure users found was wiping
# and re-extracting ledger.tar.gz. These tests pin the corrected, sidecar-clean swap.
# ---------------------------------------------------------------------------
import types

MISC_SCHEMA = "CREATE TABLE misc (block_height INTEGER, difficulty TEXT)"


class _FakeLog:
    def warning(self, *a, **k):
        pass
    info = debug = error = warning


def _fake_node(tmp_path):
    hyper = str(tmp_path / "hyper.db")
    node = types.SimpleNamespace()
    node.logger = types.SimpleNamespace(app_log=_FakeLog())
    # full-ledger recompress path: ledger_path and hyper_path are distinct; the copy source is hyper.db
    node.ledger_path = str(tmp_path / "ledger.db")
    node.hyper_path = hyper
    node.trace_db_calls = False
    return node


def _build_hyper_wal(path, heights):
    """Create a WAL-mode hyper.db, insert `heights` as reward rows, and leave committed frames stranded
    in the -wal (autocheckpoint off, connection left open) so the on-disk state mimics a hard kill."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")   # frames stay in -wal, not the main file
    conn.execute(SCHEMA)
    conn.execute(MISC_SCHEMA)
    for h in heights:
        conn.execute("INSERT INTO transactions (block_height,timestamp,address,recipient,amount,"
                     "signature,public_key,block_hash,fee,reward,operation,openfield) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (h, 1600000000 + h, "miner", "A%d" % h, "0", "s", "p", "b", "0", "10.0", "0", ""))
        conn.execute("INSERT INTO misc VALUES (?,?)", (h, "1"))
    conn.commit()
    return conn  # deliberately left open — caller closes after recompress


def _no_orphan_sidecars(path):
    return not os.path.exists(path + "-wal") and not os.path.exists(path + "-shm")


def test_recompress_clears_stale_hyper_sidecars(tmp_path):
    """A pre-existing stale hyper.db-wal/-shm (from an unclean stop) must not survive the swap."""
    node = _fake_node(tmp_path)
    # a clean hyper.db plus bogus leftover sidecars beside it
    conn = sqlite3.connect(node.hyper_path)
    conn.execute(SCHEMA)
    conn.execute(MISC_SCHEMA)
    for h in range(1, 12):
        conn.execute("INSERT INTO transactions (block_height,timestamp,address,recipient,amount,"
                     "signature,public_key,block_hash,fee,reward,operation,openfield) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (h, 1600000000 + h, "miner", "R%d" % h, "0", "s", "p", "b", "0", "10.0", "0", ""))
        conn.execute("INSERT INTO misc VALUES (?,?)", (h, "1"))
    conn.commit(); conn.close()
    with open(node.hyper_path + "-wal", "wb") as f:
        f.write(b"\x00" * 4096)          # garbage sidecars that would poison the next open
    with open(node.hyper_path + "-shm", "wb") as f:
        f.write(b"\x00" * 4096)

    chain_ops.recompress_ledger(node, depth=2)

    assert _no_orphan_sidecars(node.hyper_path), "stale hyper.db-wal/-shm survived the swap"
    assert not os.path.exists(node.ledger_path + ".temp"), "temp DB was not consumed by the swap"
    # hyper.db must open and read cleanly (no 'disk image is malformed')
    c = sqlite3.connect(node.hyper_path)
    assert int(c.execute("SELECT max(block_height) FROM transactions").fetchone()[0]) == 11
    c.close()


def test_recompress_preserves_committed_wal_frames(tmp_path):
    """The copy must include committed rows still stranded in the source WAL (checkpoint-before-copy),
    otherwise the rebuilt hyper.db is silently BEHIND the real tip."""
    node = _fake_node(tmp_path)
    writer = _build_hyper_wal(node.hyper_path, range(1, 11))  # heights 1..10 sit in the -wal
    # sanity: the frames really are stranded in the WAL, not yet in the main file
    assert os.path.exists(node.hyper_path + "-wal") and os.path.getsize(node.hyper_path + "-wal") > 0

    chain_ops.recompress_ledger(node, depth=2)
    writer.close()

    assert _no_orphan_sidecars(node.hyper_path)
    c = sqlite3.connect(node.hyper_path)
    top = int(c.execute("SELECT max(block_height) FROM transactions").fetchone()[0])
    c.close()
    assert top == 10, "committed WAL frames were dropped by the copy (top=%s, expected 10)" % top
