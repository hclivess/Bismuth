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


def test_coinbase_root_embed_extract():
    import vm_engine
    root = "a" * 64
    of = vm_engine.embed_state_root(root, "deadbeef")
    assert vm_engine.extract_state_root(of) == root            # round-trips through the coinbase openfield
    assert vm_engine.extract_state_root("hf2deadbeef") is None  # the hf2 signal is not a root
    assert vm_engine.extract_state_root("") is None
    assert vm_engine.extract_state_root("vmsr" + "b" * 60) is None  # too short to hold a 32-byte root
