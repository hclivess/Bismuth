"""
VM state root (vm_state.state_root) — determinism unit tests.

The whole point of a state root is that two nodes with identical contract state produce an IDENTICAL
root, regardless of the order operations arrived in, and a different state produces a different root.

Run with: python3 -m pytest tests/test_vm_state.py -v
"""
import pytest

pytest.importorskip("lmdb")

import vm_state


def test_state_root_is_deterministic_and_order_independent(tmp_path):
    a = vm_state.VMState(str(tmp_path / "a"))
    b = vm_state.VMState(str(tmp_path / "b"))
    try:
        # identical state, applied the SAME way
        a.deploy("c1", b"\x01\x02\x03")
        a.commit_storage("c1", {5: 99, 1: 7})
        b.deploy("c1", b"\x01\x02\x03")
        b.commit_storage("c1", {5: 99, 1: 7})
        assert a.state_root() == b.state_root()

        # identical FINAL state, applied in a DIFFERENT order -> same root
        d = vm_state.VMState(str(tmp_path / "d"))
        d.deploy("c1", b"\x01\x02\x03")
        d.commit_storage("c1", {1: 7})
        d.commit_storage("c1", {5: 99})
        assert d.state_root() == a.state_root()
        d.close()

        # different state -> different root; empty has its own fixed root
        empty = vm_state.VMState(str(tmp_path / "e"))
        assert empty.state_root() != a.state_root()
        assert len(a.state_root()) == 64                  # 32-byte hex
        a.commit_storage("c1", {5: 100})                  # change one slot
        assert a.state_root() != b.state_root()
        empty.close()
    finally:
        a.close()
        b.close()


def _exercise(s):
    """Drive the full public surface and return an observable snapshot of the store's contents."""
    s.deploy("c1", b"\x01\x02\x03")
    s.deploy("c2", b"\xaa\xbb")
    s.commit_storage("c1", {5: 99, 1: 7, 9: 0})      # slot 9 == 0 -> stored as deletion
    s.commit_storage("c2", {0: 1, 256: 1 << 200})    # big 256-bit word
    s.add_balance("c1", 500)
    s.add_balance("c1", -100)
    s.add_balance("c2", 0)                            # no entry written
    return {
        "root": s.state_root(),
        "contracts": sorted(s.list_contracts()),
        "code_c1": s.get_code("c1"),
        "code_c2": s.get_code("c2"),
        "stor_c1": s.load_storage("c1"),
        "stor_c2": s.load_storage("c2"),
        "items_c1": s.storage_items("c1"),
        "bal_c1": s.get_balance("c1"),
        "bal_c2": s.get_balance("c2"),
    }


def test_backend_swappability_lmdb_vs_sqlite(tmp_path):
    """The SAME VMState store, run on the lmdb backend and on the sqlite-kv backend, must produce
    BYTE-IDENTICAL results across the whole public surface — including the consensus state_root. This
    pins that the engine is a one-arg factory choice through the kvstore seam (doc/26 stage 1)."""
    lm = vm_state.VMState(str(tmp_path / "lmdb"), backend="lmdb")
    sq = vm_state.VMState(str(tmp_path / "sqlite"), backend="sqlite-kv")
    try:
        snap_lmdb = _exercise(lm)
        snap_sqlite = _exercise(sq)
        assert snap_lmdb == snap_sqlite
        assert len(snap_lmdb["root"]) == 64

        # clear() on both -> identical (empty) root, and the empty root is the fixed one
        lm.clear()
        sq.clear()
        assert lm.state_root() == sq.state_root()
        assert lm.list_contracts() == [] == sq.list_contracts()
    finally:
        lm.close()
        sq.close()


def test_on_disk_bytes_parity(tmp_path):
    """The migrated store keeps raw-byte keys/values — prove the exact on-disk bytes the original
    lmdb.open() store wrote are still what's persisted (a node reads its existing files unchanged)."""
    import lmdb

    s = vm_state.VMState(str(tmp_path / "s"), backend="lmdb")
    try:
        s.deploy("c1", b"\x01\x02\x03")
        s.commit_storage("c1", {5: 99})
        s.add_balance("c1", 42)
    finally:
        s.close()

    # reopen the raw lmdb env exactly as the pre-migration store did and assert the bytes
    env = lmdb.open(str(tmp_path / "s"), subdir=True, max_dbs=3, readonly=True, lock=False)
    try:
        code_db = env.open_db(b"code")
        stor_db = env.open_db(b"storage")
        bal_db = env.open_db(b"balances")
        with env.begin() as txn:
            assert txn.get(b"c1", db=code_db) == b"\x01\x02\x03"
            assert txn.get(b"c1:" + (5).to_bytes(32, "big"), db=stor_db) == (99).to_bytes(32, "big")
            assert txn.get(b"c1", db=bal_db) == (42).to_bytes(32, "big")
    finally:
        env.close()


def test_coinbase_root_embed_extract():
    import vm_engine
    root = "a" * 64
    of = vm_engine.embed_state_root(root, "deadbeef")
    assert vm_engine.extract_state_root(of) == root            # round-trips through the coinbase openfield
    assert vm_engine.extract_state_root("hf2deadbeef") is None  # the hf2 signal is not a root
    assert vm_engine.extract_state_root("") is None
    assert vm_engine.extract_state_root("vmsr" + "b" * 60) is None  # too short to hold a 32-byte root
