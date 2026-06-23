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
