"""
The LMDB ledger (block_store.BlockStore) — pure unit tests, no node/network.

One environment holds blocks + difficulty + every index; commit_block and rollback are single atomic
transactions. These tests pin: column typing, the tip+1 contiguity guard, each index (signature, address,
balances, txid, pubkey registry), rollback restoring EXACTLY the pre-commit state, reindex == incremental,
and the fixed allocations.

Run with: python3 -m pytest tests/test_ledger_store.py -v
"""
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import amounts  # noqa: E402
import block_store  # noqa: E402
from block_store import BlockStore  # noqa: E402

SMALL = 1 << 26
A, B, C = "a" * 56, "b" * 56, "c" * 56


def _tx(h, ts, sender, recipient, amount, sig, fee="0.01", reward="0", op="0", of="", pk="PK"):
    return (str(h), str(ts), sender, recipient, str(amount), sig, pk, "%056x" % h, str(fee), str(reward), op, of)


def _block(h, txs, miner=A, reward="10"):
    rows = [_tx(h, 1000 + h, *t) for t in txs]
    rows.append(_tx(h, 1000 + h, miner, miner, "0", "cb%d" % h, fee="0", reward=reward, of="nonce%d" % h))
    return rows


def _snapshot(s):
    """Every index's observable state, for exact before/after comparisons."""
    with s.store.txn() as txn:
        return {name: sorted((bytes(k), bytes(v)) for k, v in txn.iterate(getattr(s, name)))
                for name in ("blocks", "hashes", "diff", "sig", "addr", "bal", "txid", "pkref", "meta")}


@pytest.fixture
def store(tmp_path):
    s = BlockStore(str(tmp_path / "ledger"), map_size=SMALL)
    s.commit_block(1, "%056x" % 1, [_tx(1, 1000, "genesis", A, "0", "gsig", fee="0", reward="1", op="1",
                                        of="genesis")], difficulty=50)
    yield s
    s.close()


def test_typed_rows_follow_column_affinity(store):
    store.commit_block(2, "%056x" % 2, _block(2, [(A, B, "1.50000000", "s2")]), difficulty="51.5")
    r = store.get_block(2)[0]
    assert r == (2, 1002, A, B, 1.5, "s2", "PK", "%056x" % 2, 0.01, 0, "0", "")
    assert isinstance(r, tuple)
    assert block_store.typed_row(("7", "1.25", "x", "y", "3.0", "s", "p", "h", "0E-8", "2", "op", "of")) == \
        (7, 1.25, "x", "y", 3, "s", "p", "h", 0, 2, "op", "of")


def test_commit_must_extend_the_tip(store):
    with pytest.raises(ValueError):
        store.commit_block(3, "%056x" % 3, _block(3, []))
    assert store.tip() == 1


def test_balances_indexes_and_reads(store):
    store.commit_block(2, "%056x" % 2, _block(2, [(A, B, "2", "s2a"), (A, C, "1", "s2b")]), difficulty=51)
    store.commit_block(3, "%056x" % 3, _block(3, [(B, C, "0.5", "s3")], miner=B), difficulty=52)
    # A: genesis reward 1 + coinbase 10 - 3 sent - 0.02 fees
    assert store.balance(A) == Decimal("7.98000000")
    assert store.balance(B) == Decimal("11.49000000")      # 2 + 10 - 0.5 - 0.01
    assert store.balance(C) == Decimal("1.50000000")
    assert store.balance_parts(C) == (150000000, 0, 0, 0)
    assert store.tx_count() == 1 + 3 + 2
    assert store.last_difficulty() == (3, "52")
    assert store.signature_exists("s2b") and not store.signature_exists("nope")
    assert not store.signature_exists("s3", max_height=2)
    assert [r[5] for r in store.rows_by_sig_prefix("s2")] == ["s2a", "s2b"]
    assert [r[0] for r in store.address_rows(C)] == [3, 2]
    assert [r[5] for r in store.address_rows(A, role="sender", reverse=False)] == ["s2a", "s2b", "cb2"]
    assert store.first_pubkey(A) == "PK"
    assert store.reward_units_since(B, 2) == amounts.to_units(10)
    assert store.coinbase(3)[5] == "cb3"
    assert store.height_by_hash("%056x" % 3) == 3


def test_address_rows_limit_keeps_block_order(store):
    # newest block first, rows WITHIN a block in insertion order — and a limit that cuts a block keeps
    # its FIRST rows (the tx), never the coinbase that follows it (addlistlimjson / latest tx regression)
    store.commit_block(2, "%056x" % 2, _block(2, [(A, A, "1", "s2a"), (A, B, "2", "s2b")]), difficulty=51)
    store.commit_block(3, "%056x" % 3, _block(3, [(A, A, "3", "s3")]), difficulty=52)
    assert [r[5] for r in store.address_rows(A)] == ["s3", "cb3", "s2a", "s2b", "cb2", "gsig"]
    assert [r[5] for r in store.address_rows(A, limit=1)] == ["s3"]
    assert [r[5] for r in store.address_rows(A, limit=3)] == ["s3", "cb3", "s2a"]
    assert [r[5] for r in store.address_rows(A, limit=2, skip=2)] == ["s2a", "s2b"]
    assert [r[5] for r in store.address_rows(A, reverse=False, limit=2)] == ["gsig", "s2a"]


def test_rollback_restores_the_exact_prior_state(store):
    store.commit_block(2, "%056x" % 2, _block(2, [(A, B, "2", "s2")]), difficulty=51)
    before = _snapshot(store)
    store.commit_block(3, "%056x" % 3, _block(3, [(B, C, "1", "s3"), (A, C, "3", "s3b")]), difficulty=52)
    store.commit_block(4, "%056x" % 4, _block(4, [(C, A, "0.5", "s4")], miner=C), difficulty=53)
    removed = store.rollback(2)
    assert [r[5] for r in removed] == ["s3", "s3b", "cb3", "s4", "cb4"]
    assert _snapshot(store) == before
    assert store.tip() == 2


def test_reindex_matches_incremental_maintenance(store):
    for h in range(2, 8):
        store.commit_block(h, "%056x" % h, _block(h, [(A, B, "0.1", "s%d" % h)], miner=(A, B)[h % 2]),
                           difficulty=50 + h)
    incremental = _snapshot(store)
    store.reindex()
    assert _snapshot(store) == incremental


def test_allocations_credit_balances_and_survive_reindex(store):
    store.set_allocations({B: amounts.to_units(5), C: -amounts.to_units(2)})
    assert store.balance(B) == Decimal("5.00000000")
    assert store.balance(C) == Decimal("-2.00000000")
    store.set_allocations({B: amounts.to_units(5)})          # C's adjustment withdrawn
    assert store.balance(C) == 0
    store.reindex()
    assert store.balance(B) == Decimal("5.00000000")
    assert store.allocations() == {B: amounts.to_units(5)}


def test_post_fork_txid_and_pubkey_registry(store):
    fh = 2
    rows = _block(2, [(A, B, "1", "s2", "0.01", "0", "0", "", "PKA")])
    store.commit_block(2, "%056x" % 2, rows, difficulty=51, fork_height=fh)
    txid = block_store.txid_of(store.get_block_raw(2)[0], fh)
    assert store.txid_height(txid) == 2
    assert store.pkref_get(A) == b"PKA" and store.pkref_first_seen(A) == 2
    assert store.get_block(2)[0][5] == "s2"           # true-bytes storage round-trips the wire string
    store.rollback(1, fork_height=fh)
    assert store.txid_height(txid) is None and store.pkref_get(A) is None


def test_open_ledger_is_process_wide(tmp_path):
    p = str(tmp_path / "shared")
    s1 = block_store.open_ledger(p, map_size=SMALL)
    assert block_store.open_ledger(p) is s1
    block_store.close_ledger(p)


def test_mempool_is_in_memory_and_ordered(store):
    import threading
    import mempool

    class Cfg:
        version = "regnet"
        debug = False
        mandatory_message = {}

    class Log:
        def warning(self, *a): pass
        info = error = warning

    mp = mempool.Mempool(Log(), Cfg(), threading.Lock())
    mp._txs["x"] = ("1.00", A, B, "1.00000000", "x", "pk", "", "", 100)
    mp._txs["y"] = ("1.00", A, B, "5.00000000", "y", "pk", "", "", 200)
    assert [t[4] for t in mp.transactions()] == ["y", "x"]
    assert [t[4] for t in mp.transactions(since=150)] == ["y"]
    assert mp.sig_check("x") and mp.count() == 2
    mp.delete_transaction("x")
    assert not mp.sig_check("x")
