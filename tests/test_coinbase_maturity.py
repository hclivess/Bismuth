"""doc/42 coinbase maturity — unit tests for essentials.immature_coinbase (the account-model carve-out).

Run: python3 -m pytest tests/test_coinbase_maturity.py -q
"""
import sqlite3
from decimal import Decimal

import essentials


MINER = "miner_addr"
OTHER = "other_addr"


def _ledger(rows):
    """rows = list of (block_height, recipient, address, amount, reward). In-memory transactions table."""
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE transactions (block_height INTEGER, timestamp, address, recipient, "
                "amount, signature, public_key, block_hash, fee, reward, operation, openfield)")
    for h, recip, addr, amount, reward in rows:
        cur.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (h, "%.2f" % (1500000000 + h), addr, recip, amount, "s%d" % h, "p", "bh",
                     "0", reward, "0", ""))
    conn.commit()
    return cur


def test_maturity_constant_is_fixed_not_rollback_depth():
    # doc/42: fixed consensus constant, deliberately decoupled from the per-node rollback_depth (30).
    assert essentials.COINBASE_MATURITY == 100
    assert essentials.COINBASE_MATURITY != 30


def test_immature_sums_only_recent_coinbase_rewards():
    # coinbase rewards at heights 950, 990, 999 to MINER; validate at height 1000 (window: > 1000-100 = 900).
    cur = _ledger([
        (950, MINER, "coinbase", "0.00000000", "10.00000000"),   # 50 deep  -> immature
        (990, MINER, "coinbase", "0.00000000", "10.00000000"),   # 10 deep  -> immature
        (999, MINER, "coinbase", "0.00000000", "10.00000000"),   # 1 deep   -> immature
        (800, MINER, "coinbase", "0.00000000", "10.00000000"),   # 200 deep -> MATURE (excluded)
    ])
    imm = essentials.immature_coinbase(MINER, 1000, cur, 100)
    assert imm == Decimal("30.00000000"), imm   # only the three within the last 100 blocks


def test_immature_boundary_exactly_maturity_deep_is_mature():
    # a reward at exactly validate_height - COINBASE_MATURITY is mature (not > cutoff); one block newer is immature.
    cur = _ledger([
        (900, MINER, "coinbase", "0.00000000", "5.00000000"),    # 1000-100 = 900 -> mature (block_height > 900 is False)
        (901, MINER, "coinbase", "0.00000000", "7.00000000"),    # 901 > 900 -> immature
    ])
    assert essentials.immature_coinbase(MINER, 1000, cur, 100) == Decimal("7.00000000")


def test_immature_ignores_non_coinbase_and_other_addresses():
    cur = _ledger([
        (995, MINER, "sender", "3.00000000", "0"),               # reward 0 -> NOT a coinbase, ignored
        (996, OTHER, "coinbase", "0.00000000", "10.00000000"),   # different recipient, ignored
        (997, MINER, "coinbase", "0.00000000", "10.00000000"),   # counted
    ])
    assert essentials.immature_coinbase(MINER, 1000, cur, 100) == Decimal("10.00000000")
    assert essentials.immature_coinbase(OTHER, 1000, cur, 100) == Decimal("10.00000000")


def test_immature_zero_when_no_recent_rewards():
    cur = _ledger([(100, MINER, "coinbase", "0.00000000", "10.00000000")])  # 900 deep at height 1000
    assert essentials.immature_coinbase(MINER, 1000, cur, 100) == Decimal("0")
    assert essentials.immature_coinbase("nobody", 1000, cur, 100) == Decimal("0")


def test_spendable_carveout_arithmetic():
    # the carve-out the digest/mempool checks apply: spendable = balance - immature.
    cur = _ledger([(999, MINER, "coinbase", "0.00000000", "10.00000000")])
    balance = Decimal("25.00000000")             # say the miner also holds 15 mature + this 10 immature
    immature = essentials.immature_coinbase(MINER, 1000, cur, 100)
    spendable = balance - immature
    assert spendable == Decimal("15.00000000")   # can spend the mature 15, not the immature 10


def test_coinbase_maturity_is_per_network():
    # mainnet/testnet vs regnet (small, so short test chains can mine-then-spend) — a per-network consensus
    # constant, not a per-node config.
    assert essentials.coinbase_maturity(False) == essentials.COINBASE_MATURITY == 100
    assert essentials.coinbase_maturity(True) == essentials.COINBASE_MATURITY_REGNET
    assert essentials.coinbase_maturity(True) < essentials.coinbase_maturity(False)
