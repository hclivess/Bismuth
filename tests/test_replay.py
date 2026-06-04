# Replay verification on regnet: every block hash must reproduce from stored rows through the frozen
# consensus boundary, both as-stored and with amounts round-tripped through integer atomic units
# (proving the planned integer-amount storage migration is hash-safe). See doc/16 phase 2.
# Run with: python3 -m pytest -v

from time import sleep

import replay_verify


def test_replay_block_hashes_and_integer_roundtrip(client):
    client.mine(2)
    client.send(client.address, 1.0)   # a non-coinbase tx, so a block has >1 transaction
    client.mine(8)
    sleep(0.3)
    top = client.command("blocklastjson")["block_height"]
    rows = client.command("api_getblocksince", [top - 9])   # raw 12-field rows for ~9 recent blocks
    assert rows, "expected raw block rows"

    # as-stored: the serialization reproduces every stored block hash (chain integrity)
    assert replay_verify.verify_blocks(rows, integer_roundtrip=False) == []

    # integer round-trip: converting every amount to integer units and back changes no block hash
    assert replay_verify.verify_blocks(rows, integer_roundtrip=True) == []
