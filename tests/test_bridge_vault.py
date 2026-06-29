"""
contracts/bridge_vault.py — the peg-IN vault (doc/45 bridge), driven through the REAL vm_engine against a
real VMState. Proves the END-TO-END peg-in provability the bridge rests on:

  lock BIS (with an ETH recipient) -> the BIS sits in the vault's custody -> the lock record in contract
  storage is MERKLE-PROVABLE (vm_state.merkle_prove_storage) against EXACTLY the committed VM state root
  (doc/45 Stage 2b) -> which is what an Ethereum-side verifier (Stage 3) checks before minting wBIS.

Run with: python3 -m pytest tests/test_bridge_vault.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "contracts"))

import vm_engine
import vm_merkle
import bridge_vault


def H(x):
    return "%056x" % x


def test_lock_records_a_merkle_provable_entry(tmp_path):
    pytest.importorskip("lmdb")
    import vm_state
    st = vm_state.VMState(str(tmp_path / "vault"))
    try:
        vault = H(0x5A17)
        st.deploy(vault, bridge_vault.build())

        eth_recipient = bytes.fromhex("f5cb350b40726b5bcf170d12e162b6193b291b41")  # 20-byte ETH addr (live wBIS)
        amount = 1000
        of = vault + ":" + eth_recipient.hex()
        vm_engine._call(st, of, "sig", "f" * 56, vm_engine.VM_SINK, amount, 1)

        # 1) the BIS is really locked in the vault's own custody (no operator holds it)
        assert st.get_balance(vault) == amount

        # 2) the lock is recorded: counter -> 1, so this lock's base = 1*8 = 8.
        #    slot 8 = amount, slot 9 = recipient word 0, ... slot 13 = recipient word 4.
        committed = st.merkle_root()

        amt = st.merkle_prove_storage(vault, 8)
        assert amt is not None and amt["value"] == amount
        assert amt["root"] == committed                                   # proves against the COMMITTED root
        assert vm_merkle.verify_proof(bytes.fromhex(amt["root"]), amt["leaf"], amt["proof"])

        # 3) the ETH recipient is provable too (word 0 = first 4 bytes, big-endian)
        rec0 = st.merkle_prove_storage(vault, 9)
        assert rec0 is not None and rec0["value"] == int.from_bytes(eth_recipient[0:4], "big")
        assert vm_merkle.verify_proof(bytes.fromhex(rec0["root"]), rec0["leaf"], rec0["proof"])

        # 4) tampering the proven leaf breaks verification (a forged lock can't pass)
        assert not vm_merkle.verify_proof(bytes.fromhex(amt["root"]), amt["leaf"] + b"x", amt["proof"])
    finally:
        st.close()


def test_two_locks_are_independently_provable(tmp_path):
    pytest.importorskip("lmdb")
    import vm_state
    st = vm_state.VMState(str(tmp_path / "vault2"))
    try:
        vault = H(0x5A18)
        st.deploy(vault, bridge_vault.build())
        r1 = bytes.fromhex("56672ecb506301b1e32ed28552797037c54d36a9")  # BNB wBIS addr (20 bytes)
        r2 = bytes.fromhex("f4f82f8d84c529987201609cecee8ab136a50c8c")  # ETH/wBIS pool (20 bytes)
        vm_engine._call(st, vault + ":" + r1.hex(), "sig", "f" * 56, vm_engine.VM_SINK, 700, 1)
        vm_engine._call(st, vault + ":" + r2.hex(), "sig", "f" * 56, vm_engine.VM_SINK, 300, 1)

        assert st.get_balance(vault) == 1000                              # both locks held in custody
        # lock 1 amount at slot 8, lock 2 amount at slot 16
        a1 = st.merkle_prove_storage(vault, 8)
        a2 = st.merkle_prove_storage(vault, 16)
        assert a1["value"] == 700 and a2["value"] == 300
        root = st.merkle_root()
        assert a1["root"] == root and a2["root"] == root
        assert vm_merkle.verify_proof(bytes.fromhex(root), a1["leaf"], a1["proof"])
        assert vm_merkle.verify_proof(bytes.fromhex(root), a2["leaf"], a2["proof"])
    finally:
        st.close()
