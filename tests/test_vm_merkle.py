"""
vm_merkle — Merkle commitment + inclusion proofs over VM state (doc/45 bridge Stage 2).

Two layers:
  * PURE library tests (no node / no lmdb): root determinism, promotion of lone nodes, proof verify,
    tamper-rejection.
  * INTEGRATION (lmdb): the flat state_root stays BYTE-IDENTICAL after the _state_entries refactor (the
    consensus commitment is unchanged), and a storage-slot inclusion proof verifies against merkle_root.

Run with: python3 -m pytest tests/test_vm_merkle.py -v
"""
import hashlib

import pytest

import vm_merkle


# ---------------- pure library ----------------
def test_empty_root_is_fixed():
    assert vm_merkle.merkle_root([]) == vm_merkle.EMPTY_ROOT


def test_single_leaf_root_and_empty_proof():
    leaves = [b"only"]
    root = vm_merkle.merkle_root(leaves)
    assert root == vm_merkle.leaf_hash(b"only")
    proof = vm_merkle.merkle_proof(leaves, 0)
    assert proof == [] and vm_merkle.verify_proof(root, b"only", proof)


def test_every_leaf_proof_verifies_incl_odd_promotion():
    # 5 leaves -> levels of size 5,3,2,1: exercises promotion (lone node carried up) at two levels.
    leaves = [b"C-leaf-0", b"S-leaf-1", b"S-leaf-2", b"B-leaf-3", b"B-leaf-4"]
    root = vm_merkle.merkle_root(leaves)
    for i, leaf in enumerate(leaves):
        proof = vm_merkle.merkle_proof(leaves, i)
        assert vm_merkle.verify_proof(root, leaf, proof), "index %d should verify" % i


def test_tampered_leaf_and_wrong_root_fail():
    leaves = [b"a", b"b", b"c", b"d"]
    root = vm_merkle.merkle_root(leaves)
    proof = vm_merkle.merkle_proof(leaves, 2)
    assert vm_merkle.verify_proof(root, b"c", proof)
    assert not vm_merkle.verify_proof(root, b"c-tampered", proof)        # wrong leaf
    assert not vm_merkle.verify_proof(vm_merkle.leaf_hash(b"x"), b"c", proof)  # wrong root
    # a sibling-swapped proof must not verify
    if proof and proof[0][0] is not None:
        bad = [(proof[0][0], not proof[0][1])] + proof[1:]
        assert not vm_merkle.verify_proof(root, b"c", bad)


def test_root_is_deterministic_and_order_sensitive():
    a = vm_merkle.merkle_root([b"1", b"2", b"3"])
    b = vm_merkle.merkle_root([b"1", b"2", b"3"])
    c = vm_merkle.merkle_root([b"2", b"1", b"3"])
    assert a == b and a != c


# ---------------- vm_state integration ----------------
def test_state_root_unchanged_and_storage_proof_verifies(tmp_path):
    pytest.importorskip("lmdb")
    import vm_state
    s = vm_state.VMState(str(tmp_path / "s"))
    try:
        s.deploy("c1", b"\x01\x02\x03")
        s.deploy("c2", b"\xaa\xbb")
        s.commit_storage("c1", {5: 99, 1: 7})
        s.set_balance("c2", 1234)

        # 1) the flat consensus root is byte-identical to a fresh concat-hash of the entry list (no drift
        #    from the refactor — the commitment is unchanged).
        expected = hashlib.blake2b(b"".join(s._state_entries()), digest_size=32).hexdigest()
        assert s.state_root() == expected
        assert len(s.state_root()) == 64

        # 2) merkle root is over the SAME entries, distinct scheme, 32-byte hex.
        mroot = s.merkle_root()
        assert len(mroot) == 64 and mroot != s.state_root()

        # 3) an inclusion proof for a real storage slot verifies against the merkle root.
        pr = s.merkle_prove_storage("c1", 5)
        assert pr is not None and pr["value"] == 99
        assert vm_merkle.verify_proof(bytes.fromhex(pr["root"]), pr["leaf"], pr["proof"])
        # tampering the proven leaf breaks verification
        assert not vm_merkle.verify_proof(bytes.fromhex(pr["root"]), pr["leaf"] + b"x", pr["proof"])

        # 4) an unset slot (0 stored as deletion) is absent -> not provable.
        assert s.merkle_prove_storage("c1", 999) is None
    finally:
        s.close()
