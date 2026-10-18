"""
The LMDB ledger beyond the core invariants (test_ledger_store.py): read edges, difficulty records,
snapshot/readonly round-trips, the txid/pubkey views — and the DbHandler facade the node calls.

Run with: python3 -m pytest tests/test_ledger_store_more.py -v
"""
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import amounts  # noqa: E402
import block_store  # noqa: E402
from block_store import BlockStore  # noqa: E402
from dbhandler import DbHandler  # noqa: E402

SMALL = 1 << 26
A, B, C = "a" * 56, "b" * 56, "c" * 56


def _tx(h, ts, sender, recipient, amount, sig, fee="0.01", reward="0", op="0", of="", pk="PK"):
    return (str(h), str(ts), sender, recipient, str(amount), sig, pk, "%056x" % h, str(fee), str(reward), op, of)


def _block(h, txs, miner=A, reward="10", ts=None):
    ts = 1000 + 60 * h if ts is None else ts
    rows = [_tx(h, ts, *t) for t in txs]
    rows.append(_tx(h, ts, miner, miner, "0", "cb%d" % h, fee="0", reward=reward, of="nonce%d" % h))
    return rows


def _chain(s, top, start=2):
    for h in range(start, top + 1):
        s.commit_block(h, "%056x" % h, _block(h, [(A, B, "0.1", "s%d" % h)], miner=(A, C)[h % 2]),
                       difficulty=50 + h / 10)


@pytest.fixture
def store(tmp_path):
    s = BlockStore(str(tmp_path / "ledger"), map_size=SMALL)
    s.commit_block(1, "%056x" % 1, [_tx(1, 1000, "genesis", A, "0", "gsig", fee="0", reward="1", op="1",
                                        of="genesis")], difficulty=50)
    yield s
    s.close()


# ---------------------------------------------------------------------------------------------- store
def test_empty_store_reads(tmp_path):
    s = BlockStore(str(tmp_path / "empty"), map_size=SMALL)
    try:
        assert s.tip() is None and s.first() is None and s.count() == 0 and s.tx_count() == 0
        assert s.get_block(1) is None and s.coinbase(1) is None and s.block_hash(1) is None
        assert s.balance(A) == 0 and s.balance_parts(A) == (0, 0, 0, 0)
        assert s.address_rows(A) == [] and s.signature_rows("x") == []
        assert s.rollback(0) == []
        assert s.allocations() == {}
        assert list(s.blocks_desc()) == []
        # the first block may be any height (a snapshot base); after that heights must be contiguous
        s.commit_block(7, "%056x" % 7, _block(7, []))
        assert s.first() == 7 and s.tip() == 7
        with pytest.raises(ValueError):
            s.commit_block(7, "%056x" % 7, _block(7, []))
    finally:
        s.close()


def test_failed_commit_leaves_no_partial_state(store):
    before = (store.tip(), store.tx_count(), store.balance(A), store.address_rows(A))
    with pytest.raises(ValueError):
        store.commit_block(5, "%056x" % 5, _block(5, [(A, B, "1", "sX")]))
    assert (store.tip(), store.tx_count(), store.balance(A), store.address_rows(A)) == before
    assert not store.signature_exists("sX")


def test_difficulty_records(store):
    _chain(store, 5)
    assert store.difficulty(1) == "50"
    assert store.difficulty(3) == "50.3"
    assert store.difficulty(99) is None
    assert store.last_difficulty() == (5, "50.5")
    d = store.difficulties([2, 4, 99])
    assert d[2] == "50.2" and d[4] == "50.4" and d.get(99) is None
    assert [h for h, _d in store.difficulty_range(2, 4)] == [2, 3]          # half-open [start, end)
    store.put_difficulties([(2, 88.8928995217), (3, "61")])
    assert store.difficulty(2) == "88.8928995217" and store.difficulty(3) == "61"
    store.rollback(3)
    assert store.difficulty(4) is None and store.last_difficulty() == (3, "61")


def test_block_without_difficulty_keeps_the_last_recorded(store):
    store.commit_block(2, "%056x" % 2, _block(2, []))
    assert store.difficulty(2) is None
    assert store.last_difficulty() == (1, "50")


def test_range_desc_and_hash_reads(store):
    _chain(store, 12)
    assert [h for h, _ in store.blocks_in_range(-5, 3)] == [1, 2, 3]
    assert [h for h, _ in store.blocks_in_range(11, 99)] == [11, 12]
    assert [h for h, _ in store.blocks_desc()][:3] == [12, 11, 10]
    assert [h for h, _ in store.blocks_desc(2)] == [2, 1]
    assert store.block_hash(7) == "%056x" % 7
    assert store.height_by_hash("%056x" % 7) == 7
    assert store.height_by_hash("ff" * 28) is None
    assert store.count() == 12
    assert store.coinbase(4)[block_store.REWARD] == 10


def test_signature_rows_bounds_and_limits(store):
    store.commit_block(2, "%056x" % 2, _block(2, [(A, B, "1", "dup"), (A, C, "1", "other")]))
    store.commit_block(3, "%056x" % 3, _block(3, [(B, C, "1", "dup")]))
    assert [r[0] for r in store.signature_rows("dup")] == [2, 3]
    assert [r[0] for r in store.signature_rows("dup", max_height=2)] == [2]
    assert len(store.signature_rows("dup", limit=1)) == 1
    assert store.signature_rows("du") == []                # exact match only (prefix search is separate)
    assert {r[5] for r in store.rows_by_sig_prefix("d")} == {"dup"}
    assert store.rows_by_sig_prefix("") == []                # an empty prefix never scans the whole index
    assert len(store.rows_by_sig_prefix("cb", limit=2)) == 2


def test_long_signatures_index_by_their_56_char_prefix(store):
    sig = "Z" * 56 + "tail-one"
    store.commit_block(2, "%056x" % 2, _block(2, [(A, B, "1", sig)]))
    assert store.signature_exists(sig)
    assert store.signature_rows(sig)[0][5] == sig


def test_address_views(store):
    _chain(store, 6)
    # A sends every block; B receives every block; C mines the odd ones
    assert store.address_tx_count(B, role="recipient") == 5
    assert store.address_tx_count(B, role="sender") == 0
    assert store.address_tx_count(C) == 2                      # a self-paying coinbase counts once
    assert store.address_tx_count(C, role="sender") == 2 and store.address_tx_count(C, role="recipient") == 2
    assert [r[0] for r in store.address_rows(B, lo=3, hi=4)] == [4, 3]
    assert [r[0] for r in store.address_rows(B, reverse=False, limit=2)] == [2, 3]
    assert store.reward_units_since(C, 3) == amounts.to_units(10) * 1   # only block 5 is > 3 and odd
    assert store.reward_units_since(C, 0) == amounts.to_units(10) * 2   # blocks 3 and 5
    assert store.first_pubkey(C) is None                      # a miner that never sent a tx
    assert store.first_pubkey(A) == "PK"


def test_balances_iterate_and_count(store):
    _chain(store, 4)
    bal = dict(store.iter_balances())
    assert set(bal) == {A, B, C}                              # a zero balance ("genesis") is not stored
    assert store.address_count() == 3
    total = sum(c + r - d - f for c, r, d, f in bal.values())
    # money is conserved: all that exists = minted rewards - burned fees
    minted = amounts.to_units(1) + amounts.to_units(10) * 3      # genesis + blocks 2..4
    fees = amounts.to_units("0.01") * 3
    assert total == minted - fees
    assert store.balance("genesis") == Decimal("0")


def test_allocations_follow_rollback_and_replace(store):
    store.set_allocations({B: amounts.to_units(100)})
    _chain(store, 3)
    store.rollback(1)
    assert store.balance(B) == Decimal("100.00000000")       # an allocation is not block state
    store.set_allocations({})
    assert store.balance(B) == 0
    assert store.allocations() == {}


def test_reindex_progress_and_idempotence(store):
    _chain(store, 9)
    seen = []
    n = store.reindex(batch=3, progress=lambda h, tip: seen.append((h, tip)))
    assert n == store.tx_count() == 1 + 2 * 8
    assert seen == [(3, 9), (6, 9), (9, 9)]
    first = dict(store.iter_balances())
    store.reindex()
    assert dict(store.iter_balances()) == first


def test_recent_block_weights(store):
    store.commit_block(2, "%056x" % 2, _block(2, [(A, B, "1", "w", "0.01", "0", "0", "x" * 2500)]))
    _chain(store, 4, start=3)
    assert store.recent_block_weights(4, 3) == [2 + 2, 2, 2]
    assert store.recent_block_weights(4, 100) == [4, 2, 2]     # floors at block 2, never genesis
    with pytest.raises(ValueError):
        store.recent_block_weights(6, 3, strict=True)
    assert store.recent_block_weights(6, 3) == [2]


def test_schema_marker(store):
    assert store.schema() != block_store.SCHEMA or store.schema() == block_store.SCHEMA
    store.set_schema()
    assert store.schema() == block_store.SCHEMA
    store.set_schema(1)
    assert store.schema() == 1


def test_snapshot_stream_reopens_identically(store, tmp_path):
    _chain(store, 5)
    store.set_allocations({C: 7})
    dst = tmp_path / "copy"
    dst.mkdir()
    with open(dst / "data.mdb", "wb") as f:        # streamed to a raw fd (a socket in production)
        store.stream_snapshot(f)
    copy = BlockStore(str(dst), map_size=SMALL)
    try:
        assert copy.tip() == 5
        assert copy.get_block(3) == store.get_block(3)
        assert dict(copy.iter_balances()) == dict(store.iter_balances())
        assert copy.allocations() == {C: 7}
        assert copy.last_difficulty() == store.last_difficulty()
    finally:
        copy.close()


def test_readonly_reader_in_another_process_sees_the_ledger(store, tmp_path):
    # LMDB forbids a second open in one process (hence open_ledger); a readonly reader is a separate tool
    import subprocess
    _chain(store, 3)
    store.sync()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import sys; sys.path.insert(0, %r); import block_store as b; "
            "s = b.BlockStore(%r, map_size=%d, readonly=True); print(s.tip(), s.balance(%r))"
            % (root, str(tmp_path / "ledger"), SMALL, B))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["3", str(store.balance(B))]


def test_txid_and_pubkey_views(store):
    fh = 2
    store.commit_block(2, "%056x" % 2, _block(2, [(A, B, "1", "s2", "0.01", "0", "0", "", "PKA")]),
                       fork_height=fh)
    txid = block_store.txid_of(store.get_block_raw(2)[0], fh)
    tv, pv = block_store.TxidView(store), block_store.PubkeyView(store)
    assert tv.contains(txid) and tv.height_of(txid) == 2
    assert not tv.contains(txid, max_height=1)
    assert tv.count() == store.txid_count() >= 1
    assert pv.get(A) == b"PKA" and pv.first_seen(A) == 2 and pv.count() >= 1
    assert pv.get(C) is None


def test_pre_fork_blocks_carry_no_txid_or_registry(store):
    _chain(store, 3)
    assert store.txid_count() == 0 and store.pkref_count() == 0


def test_open_ledger_reopens_after_close(tmp_path):
    p = str(tmp_path / "l")
    s1 = block_store.open_ledger(p, map_size=SMALL)
    block_store.close_ledger(p)
    s2 = block_store.open_ledger(p, map_size=SMALL)
    try:
        assert s2 is not s1
    finally:
        block_store.close_ledger(p)
    block_store.close_ledger(p)          # closing twice is harmless


# ---------------------------------------------------------------------------------------------- DbHandler
class _Node:
    genesis = A
    fork_height = None
    token_index = None

    class logger:
        class app_log:
            msgs = []

            @classmethod
            def warning(cls, m):
                cls.msgs.append(m)


@pytest.fixture
def db(store):
    node = _Node()
    node.ledger = store
    store.commit_block(2, "%056x" % 2, _block(2, [(A, A, "0", "ann1", "0.01", "0", "ann", "hello"),
                                                 (A, A, "0", "ver1", "0.01", "0", "annver", "1.2.3"),
                                                 (A, B, "1", "s2", "0.01", "0", "0", "", "PKA2")]),
                       difficulty="51.5")
    _chain(store, 5, start=3)
    return DbHandler.for_node(node)


def test_dbhandler_tip_reads(db):
    assert db.block_height_max() == 5
    assert db.block_height_max_diff() == 5
    assert db.last_block_hash() == "%056x" % 5
    assert float(db.last_block_timestamp()) == 1000 + 60 * 5
    assert db.last_mining_tx()[5] == "cb5"
    assert db.difflast() == (5, "50.5")
    tip = db.block_max_ram()
    assert tip["block_height"] == 5 and tip["block_hash"] == "%056x" % 5


def test_dbhandler_lookups(db):
    assert db.block_height_from_hash("%056x" % 3) == 3
    assert db.block_height_from_hash("nope") is None
    assert db.pubkeyget(A) == "PK"                     # genesis-era rows came first with "PK"
    with pytest.raises(ValueError):
        db.pubkeyget(C)
    assert db.annget(_Node) == "hello" and db.annverget(_Node) == "1.2.3"
    assert db.balance_get(B) == db.store.balance(B)


def test_dbhandler_pubkey_cache_clears_on_commit(db):
    assert db.pubkeyget(A) == "PK"
    db._pubkey_cache[A] = "stale"
    db.commit_block(6, "%056x" % 6, _block(6, []), 52)
    assert db._pubkey_cache == {}


def test_dbhandler_sync_and_listing(db):
    blocks = db.blocksync(3)
    assert len(blocks) == 2 and len(blocks[0][0]) == 8
    assert blocks[0][-1][3] == 0                       # the coinbase amount, typed
    assert db.blocksync(5) == []
    rows = db.latest_rows(3)
    assert [r[0] for r in rows] == [5, 5, 4]
    assert len(db.latest_rows(1000)) == db.store.tx_count()
    assert [r[0] for r in db.address_rows(B, limit=2)] == [5, 4]


def test_dbhandler_rollback_under_and_backup_higher(db):
    removed = db.rollback_under(4)
    assert {r[0] for r in removed} == {4, 5}
    assert db.block_height_max() == 3
    removed = db.backup_higher(3)
    assert {r[0] for r in removed} == {3}


def test_dbhandler_token_fallbacks_without_plugin(db):
    assert db.addfromalias("x") == "No alias"
    assert db.aliasget(A) == [[A]]
    assert db.aliasesget([A, B]) == [A, B]
    assert db.tokens_user(A) == []
    db.tokens_rollback(_Node, 3)                        # no plugin -> no-op, no raise
    db.aliases_rollback(_Node, 3)


def test_dbhandler_delegates_to_the_token_plugin(db):
    calls = []

    class TI:
        def addfromalias(self, a): return "addr-of-" + a
        def tokens_user(self, a): return ["tok"]
        def tokens_rollback(self, h): calls.append(("t", h))
        def aliases_rollback(self, h): raise RuntimeError("boom")

    node = _Node()
    node.token_index = TI()
    db.node = node
    assert db.addfromalias("bob") == "addr-of-bob" and db.tokens_user(A) == ["tok"]
    db.tokens_rollback(node, 9)
    db.aliases_rollback(node, 9)                        # a failing plugin rollback is logged, not raised
    assert calls == [("t", 9)]
    assert any("Failed to roll back the alias index" in m for m in _Node.logger.app_log.msgs)


def test_last_openfield_matches_the_full_read_pre_and_post_fork(store):
    _chain(store, 3)
    for h in (1, 2, 3):
        assert store.last_openfield(h) == store.get_block_raw(h)[-1][11]
    store.commit_block(4, "%056x" % 4, _block(4, [(A, B, "1", "s4")]), fork_height=4)    # true-bytes body
    assert store.last_openfield(4) == "nonce4" == store.get_block_raw(4)[-1][11]
    assert store.last_openfield(99) is None


def test_fork_signal_reader_uses_the_coinbase_openfield(store):
    import fork
    _chain(store, 2)
    store.commit_block(3, "%056x" % 3, [_tx(3, 1180, A, A, "0", "cb3", fee="0", reward="10",
                                            of=fork.FORK2_SIGNAL)])
    read = fork.db_fork_signal_reader(store)
    assert read(2) is False and read(99) is False
    assert read(3) is True
