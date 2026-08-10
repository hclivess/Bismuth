"""
Maintained balance index — live integration on regnet (balance_index=True).

The node maintains `node.balance_index` on every block commit (digest hook) and rebuilds it on rollback
(chain_ops, via the unified path). This reads the maintained index concurrently and asserts it stays
**bit-identical to the authoritative `ledger_balance3`** for EVERY address — through commits AND a reorg
(including the concluded dev/HN reward mirrors baked into balances). Display-only: consensus still uses
`ledger_balance3`, so a wrong index can never enable spending.

Run with: python3 -m pytest tests/test_balance_index_live.py -v
"""
import sqlite3
from time import sleep

import pytest

pytest.importorskip("lmdb")

import balance_index

INDEX = "static/balanceindex-regmode.db"
LEDGER = "file:static/regmode.db?mode=ro"
RECIP = "0badcafe" * 7


def _sql_balance_units(conn, addr):
    c = conn.execute("SELECT COALESCE(SUM(amount+reward),0) FROM transactions WHERE recipient=?",
                     (addr,)).fetchone()[0]
    d = conn.execute("SELECT COALESCE(SUM(amount+fee),0) FROM transactions WHERE address=?",
                     (addr,)).fetchone()[0]
    return int(c) - int(d)


def _check_index_matches_authoritative():
    conn = sqlite3.connect(LEDGER, uri=True, timeout=20)
    conn.text_factory = str
    bi = balance_index.BalanceIndex(INDEX, readonly=True)
    try:
        addrs = {a for (a,) in conn.execute("SELECT DISTINCT address FROM transactions")} | \
                {a for (a,) in conn.execute("SELECT DISTINCT recipient FROM transactions")}
        for a in addrs:
            assert bi.get_balance_units(a) == _sql_balance_units(conn, a), \
                "maintained index != ledger_balance3 for %s" % a
    finally:
        bi.close()
        conn.close()


def test_maintained_index_matches_through_commits(client):
    client.mine(12)                  # mints dev/HN reward mirrors (every 10 blocks on regnet) + advances
    client.send(RECIP, 1.5)
    client.mine(2)
    sleep(0.5)
    _check_index_matches_authoritative()   # the MAINTAINED (not rebuilt) index is correct


def test_maintained_index_matches_after_rollback(client):
    client.mine(4)
    sleep(0.3)
    tip = client.block_height()
    client.rollback(tip - 2)         # reorg -> chain_ops rebuilds the index from the rolled-back ledger
    sleep(0.4)
    _check_index_matches_authoritative()   # still bit-identical after the reorg
