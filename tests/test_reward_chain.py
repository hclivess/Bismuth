"""
Reward sidechain tests (reward_chain.RewardChain).

Proves the core safety property of moving the locally-minted dev/hypernode reward rows out of the main
ledger's negative-height "mirror" rows into a separate store:

    full-ledger balance(addr)  ==  positive-height-only balance(addr)  +  reward-sidechain delta(addr)

for EVERY address — i.e. the rework is exactly balance-preserving. Synthetic data + the live regnet
node (which mints these rewards itself), plus rollback.

Run with: python3 -m pytest tests/test_reward_chain.py -v
"""
import sqlite3
from time import sleep

import pytest

pytest.importorskip("lmdb")

from reward_chain import RewardChain


def _bal(conn, addr, where=""):
    c = conn.execute("SELECT COALESCE(SUM(amount+reward),0) FROM transactions WHERE recipient=? " + where,
                     (addr,)).fetchone()[0]
    d = conn.execute("SELECT COALESCE(SUM(amount+fee),0) FROM transactions WHERE address=? " + where,
                     (addr,)).fetchone()[0]
    return int(c) - int(d)


def _all_addresses(conn):
    return {a for (a,) in conn.execute("SELECT DISTINCT address FROM transactions")} | \
           {a for (a,) in conn.execute("SELECT DISTINCT recipient FROM transactions")}


def _make_ledger_with_mirrors(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, "
                 "amount INTEGER, signature, public_key, block_hash, fee INTEGER, reward INTEGER, "
                 "operation, openfield)")
    rows = [
        # positive (synced) blocks
        [1, "t", "miner", "miner", 0, "s", "p", "h1", 0, 500000000, "", ""],
        [2, "t", "miner", "alice", 100000000, "s", "p", "h2", 1000000, 0, "", ""],
        [3, "t", "alice", "bob", 30000000, "s", "p", "h3", 1000000, 0, "", ""],
        # negative-height "mirror" rows: locally-minted dev + hypernode rewards
        [-2, "t", "Development Reward", "genesis", 20000000, "0", "0", "mh2", 0, 0, "0", "0"],
        [-2, "t", "Hypernode Payouts", "hnaddr", 240000000, "0", "0", "mh2", 0, 0, "0", "0"],
        [-3, "t", "Development Reward", "genesis", 20000000, "0", "0", "mh3", 0, 0, "0", "0"],
    ]
    conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_sidechain_is_balance_equivalent_synthetic(tmp_path):
    ledger = str(tmp_path / "ledger.db")
    _make_ledger_with_mirrors(ledger)
    rc = RewardChain(str(tmp_path / "rc"), map_size=64 * 1024 * 1024)
    try:
        assert rc.extract_from_ledger(ledger) == 3
        conn = sqlite3.connect(ledger)
        try:
            for a in _all_addresses(conn):
                full = _bal(conn, a)
                positive = _bal(conn, a, "AND block_height > 0")
                assert full == positive + rc.balance_delta_units(a), \
                    "sidechain not balance-equivalent for %s" % a
        finally:
            conn.close()
        # spot-check: genesis got both dev rewards via the sidechain, not the main ledger
        assert rc.balance_delta_units("genesis") == 40000000
        assert rc.balance_delta_units("hnaddr") == 240000000
    finally:
        rc.close()


def test_rollback_drops_higher_blocks(tmp_path):
    rc = RewardChain(str(tmp_path / "rc"), map_size=64 * 1024 * 1024)
    try:
        rc.add(5, "Development Reward", "genesis", 20000000, "mh5")
        rc.add(6, "Hypernode Payouts", "hn", 240000000, "mh6")
        assert rc.balance_delta_units("hn") == 240000000
        assert rc.rollback(5) == 1               # block 6 dropped
        assert rc.balance_delta_units("hn") == 0
        assert rc.balance_delta_units("genesis") == 20000000
    finally:
        rc.close()


def test_sidechain_equivalent_on_regnet(client, tmp_path):
    client.mine(4)
    sleep(0.4)
    rc = RewardChain(str(tmp_path / "rc"), map_size=128 * 1024 * 1024)
    try:
        rc.extract_from_ledger("static/regmode.db")   # regnet mints dev/HN rewards as mirror rows
        conn = sqlite3.connect("file:static/regmode.db?mode=ro", uri=True, timeout=20)
        conn.text_factory = str
        try:
            for a in _all_addresses(conn):
                full = _bal(conn, a)
                positive = _bal(conn, a, "AND block_height > 0")
                assert full == positive + rc.balance_delta_units(a), \
                    "regnet sidechain not balance-equivalent for %s" % a
        finally:
            conn.close()
    finally:
        rc.close()
