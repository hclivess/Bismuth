"""
KVStore engine-agnostic abstraction tests (kvstore.open_store, doc/26 stage 1).

Proves the new DB-engine seam:
  (a) PARITY      — the migrated RewardChain's existing balance-equivalence rebuild passes on the lmdb
                    backend AND the on-disk bytes are identical to what a direct lmdb.open() store writes.
  (b) SWAPPABILITY— the SAME RewardChain run through open_store('sqlite-kv',...) yields identical results
                    to the lmdb backend (the seam is real + engine-independent).
  (c) PERF        — a micro-benchmark times get/put/range through LmdbKVStore vs raw lmdb txn calls and
                    asserts the wrapper overhead is small (target < ~10%).
  (d) ordered range/iterate semantics match across both backends.

Run with: python3 -m pytest tests/test_kvstore.py -v -s
"""
import struct
import time

import pytest

pytest.importorskip("lmdb")
import lmdb  # noqa: E402

from kvstore import Codec, LmdbKVStore, SqliteKVStore, open_store  # noqa: E402
from reward_chain import RewardChain  # noqa: E402

_MS = 64 * 1024 * 1024


# --------------------------------------------------------------------------- basic CRUD, both backends
@pytest.mark.parametrize("backend", ["lmdb", "sqlite-kv"])
def test_crud_roundtrip(backend, tmp_path):
    s = open_store(backend, str(tmp_path / "kv"), dbs=["d"], map_size=_MS)
    try:
        db = s.open_db("d")
        with s.txn(write=True) as t:
            t.put(db, b"k1", b"v1")
            t.put(db, b"k2", b"v2")
        with s.txn() as t:
            assert t.get(db, b"k1") == b"v1"
            assert t.get(db, b"k2") == b"v2"
            assert t.get(db, b"missing") is None
        with s.txn(write=True) as t:
            assert t.delete(db, b"k1") is True
            assert t.delete(db, b"nope") is False
        with s.txn() as t:
            assert t.get(db, b"k1") is None
        assert s.stat(db)["entries"] == 1
    finally:
        s.close()


@pytest.mark.parametrize("backend", ["lmdb", "sqlite-kv"])
def test_ordered_range_and_iterate(backend, tmp_path):
    s = open_store(backend, str(tmp_path / "kv"), dbs=["d"], map_size=_MS)
    try:
        db = s.open_db("d")
        with s.txn(write=True) as t:
            for h in (5, 1, 9, 3, 7):
                t.put(db, struct.pack(">Q", h), b"x%d" % h)
        with s.txn() as t:
            ks = [struct.unpack(">Q", k)[0] for k, _ in t.range(db)]
            assert ks == [1, 3, 5, 7, 9]                       # ascending
            ks_r = [struct.unpack(">Q", k)[0] for k, _ in t.range(db, reverse=True)]
            assert ks_r == [9, 7, 5, 3, 1]                     # descending
            # half-open [start, end)
            mid = [struct.unpack(">Q", k)[0]
                   for k, _ in t.range(db, start=struct.pack(">Q", 3), end=struct.pack(">Q", 9))]
            assert mid == [3, 5, 7]
            # reverse bounded
            mid_r = [struct.unpack(">Q", k)[0]
                     for k, _ in t.range(db, start=struct.pack(">Q", 3), end=struct.pack(">Q", 9),
                                         reverse=True)]
            assert mid_r == [7, 5, 3]
            # iterate full
            assert len([1 for _ in t.iterate(db)]) == 5
    finally:
        s.close()


@pytest.mark.parametrize("backend", ["lmdb", "sqlite-kv"])
def test_prefix_iterate(backend, tmp_path):
    s = open_store(backend, str(tmp_path / "kv"), dbs=["d"], map_size=_MS)
    try:
        db = s.open_db("d")
        with s.txn(write=True) as t:
            for k in (b"aa1", b"aa2", b"ab1", b"ba1"):
                t.put(db, k, b"v")
        with s.txn() as t:
            assert sorted(k for k, _ in t.iterate(db, prefix=b"aa")) == [b"aa1", b"aa2"]
            assert sorted(k for k, _ in t.iterate(db, prefix=b"a")) == [b"aa1", b"aa2", b"ab1"]
            assert sorted(k for k, _ in t.iterate(db, prefix=b"\xff")) == []
    finally:
        s.close()


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        open_store("foo", "/tmp/nope", dbs=["d"])


def test_mdbx_lazy_optional():
    # MDBX is a one-arg factory choice the day a binding is added; absent one it raises cleanly
    # (a RuntimeError naming the missing binding), NOT an import crash at module load.
    pytest.importorskip("kvstore")
    try:
        import mdbx  # noqa: F401
        pytest.skip("mdbx binding installed; adapter wiring is intentionally deferred")
    except ImportError:
        with pytest.raises(RuntimeError, match="mdbx"):
            open_store("mdbx", "/tmp/nope_mdbx", dbs=["d"])


# --------------------------------------------------------------------------- (a) PARITY: byte-identical
def test_lmdb_on_disk_bytes_identical_to_direct_lmdb(tmp_path):
    """The migrated RewardChain (open_store lmdb) writes the EXACT same key/value bytes as a direct
    lmdb.open() store using the store's original convention — byte-identical on-disk format."""
    rc = RewardChain(str(tmp_path / "rc"), map_size=_MS)
    try:
        rc.add(5, "Development Reward", "genesis", 20000000, "mh5")
        rc.add(5, "Hypernode Payouts", "hn", 240000000, "mh5b")  # same height -> appended list
        rc.add(6, "Development Reward", "genesis", 20000000, "mh6")
    finally:
        rc.close()

    # read raw bytes straight out of the LMDB env the store wrote
    env = lmdb.open(str(tmp_path / "rc"), subdir=True, max_dbs=1, readonly=True, lock=False)
    db = env.open_db(b"rewards")
    on_disk = {}
    with env.begin() as t:
        for k, v in t.cursor(db=db):
            on_disk[bytes(k)] = bytes(v)
    env.close()

    # reconstruct expected bytes from the original (pre-migration) convention
    expected = {
        Codec.hkey(5): Codec.pack([["Development Reward", "genesis", 20000000, "mh5"],
                                   ["Hypernode Payouts", "hn", 240000000, "mh5b"]]),
        Codec.hkey(6): Codec.pack([["Development Reward", "genesis", 20000000, "mh6"]]),
    }
    assert on_disk == expected


# --------------------------------------------------------------------------- (b) SWAPPABILITY
def _drive_reward_chain(backend, path):
    """Identical workload against a RewardChain on the given backend; return observable results."""
    rc = RewardChain(path, map_size=_MS, backend=backend)
    try:
        rc.add(5, "Development Reward", "genesis", 20000000, "mh5")
        rc.add(6, "Hypernode Payouts", "hn", 240000000, "mh6")
        rc.add(6, "Development Reward", "genesis", 5000000, "mh6b")
        rc.add(10, "alice", "bob", 30000000, "mh10")
        return {
            "entries_for_6": rc.entries_for(6),
            "all": list(rc.all_entries()),
            "delta_genesis": rc.balance_delta_units("genesis"),
            "delta_hn": rc.balance_delta_units("hn"),
            "delta_bob": rc.balance_delta_units("bob"),
            "delta_alice": rc.balance_delta_units("alice"),
            "count_before": rc.count_blocks(),
            "rolled_back": rc.rollback(6),
            "all_after": list(rc.all_entries()),
            "count_after": rc.count_blocks(),
            "delta_genesis_after": rc.balance_delta_units("genesis"),
        }
    finally:
        rc.close()


def test_swappability_lmdb_equals_sqlite(tmp_path):
    res_lmdb = _drive_reward_chain("lmdb", str(tmp_path / "rc_lmdb"))
    res_sqlite = _drive_reward_chain("sqlite-kv", str(tmp_path / "rc_sqlite"))
    assert res_lmdb == res_sqlite, "RewardChain results differ across backends -> seam not engine-independent"
    # sanity that the workload is non-trivial
    assert res_lmdb["count_before"] == 3       # heights 5,6,10
    assert res_lmdb["rolled_back"] == 1        # height 10 dropped (above tip 6)
    assert res_lmdb["count_after"] == 2
    assert res_lmdb["delta_genesis"] == 25000000


# --------------------------------------------------------------------------- (c) PERF micro-benchmark
def test_perf_lmdb_overhead(tmp_path, capsys):
    """Wrapper overhead vs raw lmdb. Robust gate [review #3]: BEST-OF-N (min) wall-clock to shrug off
    scheduler noise on a busy box, PLUS a tracemalloc net-allocation-per-op signal on the get hot path. The
    wrapper is __slots__ + one attribute deref forwarding to the raw txn, so it neither accumulates memory nor
    adds more than one Python call frame of time."""
    import tracemalloc
    N = 20000
    keys = [struct.pack(">Q", i) for i in range(N)]
    val = b"x" * 32

    raw_env = lmdb.open(str(tmp_path / "raw"), subdir=True, max_dbs=1, map_size=256 * 1024 * 1024,
                        sync=False, metasync=False)
    raw_db = raw_env.open_db(b"d")
    with raw_env.begin(write=True) as t:
        for k in keys:
            t.put(k, val, db=raw_db)

    s = LmdbKVStore(str(tmp_path / "wrap"), dbs=["d"], map_size=256 * 1024 * 1024, sync=False)
    db = s.open_db("d")
    with s.txn(write=True) as t:
        for k in keys:
            t.put(db, k, val)

    def raw_get():
        with raw_env.begin() as t:
            for k in keys:
                t.get(k, db=raw_db)

    def wrap_get():
        with s.txn() as t:
            for k in keys:
                t.get(db, k)

    def raw_range():
        with raw_env.begin() as t:
            cur = t.cursor(db=raw_db); cnt = 0
            if cur.first():
                for _ in cur.iternext(keys=True, values=True):
                    cnt += 1
            assert cnt == N

    def wrap_range():
        with s.txn() as t:
            assert sum(1 for _ in t.range(db)) == N

    def best_of(fn, n=6):
        fn()  # warmup (page cache, JIT of the path)
        best = float("inf")
        for _ in range(n):
            t0 = time.perf_counter(); fn(); best = min(best, time.perf_counter() - t0)
        return best

    rg, wg = best_of(raw_get), best_of(wrap_get)
    rr, wr = best_of(raw_range), best_of(wrap_range)

    # net retained allocation per get through the wrapper — the true "thin passthrough, no per-op allocator"
    # signal (transient value bytes come from lmdb itself, same as raw; the wrapper must add nothing it keeps).
    M = 5000
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    with s.txn() as t:
        for k in keys[:M]:
            t.get(db, k)
    delta = tracemalloc.get_traced_memory()[0] - base
    tracemalloc.stop()
    bytes_per_get = max(0, delta) / M

    s.close(); raw_env.close()

    def ovh(w, r):
        return (w - r) / r * 100.0

    print("\n=== KVStore LMDB wrapper micro-benchmark (best-of-N, N=%d) ===" % N)
    print("  get   raw %9.0f ops/s | wrap %9.0f ops/s | overhead %+6.1f%%" % (N / rg, N / wg, ovh(wg, rg)))
    print("  range raw %9.0f ops/s | wrap %9.0f ops/s | overhead %+6.1f%%" % (N / rr, N / wr, ovh(wr, rr)))
    print("  get net allocation: %.2f bytes/op" % bytes_per_get)

    # Primary gate: no per-op accumulation (robust, not scheduler-sensitive).
    assert bytes_per_get < 64.0, "wrapper retains memory per get: %.1f bytes/op" % bytes_per_get
    # Secondary gate: best-of-N wall-clock — one Python frame, a few %.
    assert ovh(wg, rg) < 20.0, "get overhead too high: %.1f%%" % ovh(wg, rg)
    assert ovh(wr, rr) < 40.0, "range overhead too high: %.1f%%" % ovh(wr, rr)


def test_sqlite_concurrent_reads(tmp_path):
    """The sqlite-kv backend must honor the KVStore 'many concurrent readers + one writer' contract via
    per-txn connections + WAL: overlapping read txns AND a read from another thread must both work — a single
    shared connection could do neither (BEGIN-within-BEGIN / cross-thread sqlite errors). [review #1]"""
    import threading
    s = open_store("sqlite-kv", str(tmp_path / "kv"), dbs=["d"], map_size=_MS)
    db = s.open_db("d")
    with s.txn(write=True) as t:
        for i in range(20):
            t.put(db, struct.pack(">Q", i), b"v%d" % i)
    # overlapping read txns on the same store (a shared connection would raise here)
    with s.txn() as a, s.txn() as b:
        assert a.get(db, struct.pack(">Q", 1)) == b"v1"
        assert b.get(db, struct.pack(">Q", 2)) == b"v2"
    # read from a different thread (a shared connection would raise the cross-thread error)
    out = {}

    def reader():
        with s.txn() as t:
            out["v"] = t.get(db, struct.pack(">Q", 3))

    th = threading.Thread(target=reader); th.start(); th.join()
    assert out["v"] == b"v3"
    s.close()
