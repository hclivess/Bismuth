# Proves the LIVE node (not just the offline migration tool) stores amounts as integer atomic units
# when ledger_integer_amounts is enabled (it is, in tests/config_custom.txt). Reads the running
# regnet ledger file directly. See doc/16 phase 2 cutover. Run with: python3 -m pytest -v

import os
import sqlite3
from time import sleep

REGNET_DB = os.path.join(os.path.dirname(__file__), "..", "static", "regmode.db")


def test_live_node_stores_integer_units(client):
    client.send(client.address, 2.5)   # 2.5 BIS -> must land as 250_000_000 atomic units on disk
    client.mine(2)
    sleep(0.3)
    conn = sqlite3.connect(REGNET_DB)   # second reader; WAL allows concurrent reads with the node
    try:
        cur = conn.cursor()
        cur.execute("SELECT type FROM pragma_table_info('transactions') WHERE name = 'amount'")
        assert cur.fetchone()[0] == "INTEGER"
        cur.execute("SELECT amount FROM transactions WHERE recipient = ? AND amount > 0 "
                    "ORDER BY block_height DESC LIMIT 1", (client.address,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 250_000_000
    finally:
        conn.close()
