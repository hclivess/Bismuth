"""Proves the live regnet node stores amounts as integer atomic units when ledger_integer_amounts is on."""
# Proves the LIVE node stores amounts as integer atomic units when ledger_integer_amounts is enabled (it is,
# in tests/config_custom.toml). Reads the running regnet LMDB ledger directly (a read-only reader alongside
# the node's writer). See doc/16 phase 2 cutover. Run with: python3 -m pytest -v

import os
from time import sleep

import amounts
import block_store

STORE = os.path.join(os.path.dirname(__file__), "..", "static", "blockstore-regmode.db")


def test_live_node_stores_integer_units(client):
    client.send(client.address, 2.5)   # 2.5 BIS -> must land as 250_000_000 atomic units on disk
    client.mine(2)
    sleep(0.3)
    amounts.LEDGER_INTEGER = True      # read in the node's storage mode (conftest restores the flag)
    bs = block_store.BlockStore(STORE, readonly=True)
    try:
        rows = [r for r in bs.address_rows(client.address, role="recipient", limit=50)
                if r[block_store.REWARD] == 0 and r[block_store.AMOUNT] != 0]
        assert rows, "the 2.5 BIS self-transfer is not in the ledger"
        assert rows[0][block_store.AMOUNT] == 250_000_000          # newest first: integer units, not 2.5
        assert bs.balance_parts(client.address)[0] % 1 == 0
    finally:
        bs.close()
