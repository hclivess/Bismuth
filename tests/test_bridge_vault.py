"""
contracts/bridge_vault.py — the peg-IN vault (doc/45 bridge), driven through the REAL vm_engine against a
real VMState. Proves the END-TO-END peg-in provability the bridge rests on:

  lock BIS (with an ETH recipient) -> the BIS sits in the vault's custody -> the lock record in contract
  storage is MERKLE-PROVABLE (vm_state.merkle_prove_storage) against EXACTLY the committed VM state root
  (doc/45 Stage 2b) -> which is what an Ethereum-side verifier (BismuthBridge / Stage 3) checks before
  minting wBIS.

Storage layout (per lock id n, base = n*16): slot[base+0] = amount; slot[base+1..10] = the 20-byte ETH
recipient as ten 16-bit big-endian chunks, each stored as (0x10000 | chunk) so it is never 0 (a 0 value is
dropped as a deletion by commit_storage and would be unprovable -> stranded BIS). See bridge_vault.py.

Run with: python3 -m pytest tests/test_bridge_vault.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "contracts"))

import vm_engine
import vm_merkle
import bridge_vault

SENTINEL = bridge_vault.SENTINEL  # 0x10000


def H(x):
    return "%056x" % x


def _chunk(recipient, j):
    """Expected stored value of recipient chunk j: (0x10000 | the j-th big-endian 16-bit chunk)."""
    return SENTINEL | int.from_bytes(recipient[2 * j:2 * j + 2], "big")


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

        # 2) the lock is recorded: counter -> 1, so this lock's base = 1*16 = 16.
        #    slot 16 = amount, slot 17 = recipient chunk 0, ... slot 26 = recipient chunk 9.
        committed = st.merkle_root()

        amt = st.merkle_prove_storage(vault, 16)
        assert amt is not None and amt["value"] == amount
        assert amt["root"] == committed                                   # proves against the COMMITTED root
        assert vm_merkle.verify_proof(bytes.fromhex(amt["root"]), amt["leaf"], amt["proof"])

        # 3) every recipient chunk is provable; chunk 0 = first 2 bytes, big-endian, with the sentinel bit set
        rec0 = st.merkle_prove_storage(vault, 17)
        assert rec0 is not None and rec0["value"] == _chunk(eth_recipient, 0)
        assert vm_merkle.verify_proof(bytes.fromhex(rec0["root"]), rec0["leaf"], rec0["proof"])
        for j in range(10):                                              # ALL ten chunks reconstruct the address
            rec = st.merkle_prove_storage(vault, 17 + j)
            assert rec is not None and rec["value"] == _chunk(eth_recipient, j)
            assert (rec["value"] >> 16) == 1                             # sentinel bit present
        # reassemble the address from the proven chunks == the original recipient
        rebuilt = b"".join((st.merkle_prove_storage(vault, 17 + j)["value"] & 0xFFFF).to_bytes(2, "big")
                           for j in range(10))
        assert rebuilt == eth_recipient

        # 4) tampering the proven leaf breaks verification (a forged lock can't pass)
        assert not vm_merkle.verify_proof(bytes.fromhex(amt["root"]), amt["leaf"] + b"x", amt["proof"])
    finally:
        st.close()


def test_zero_chunk_recipient_is_still_provable(tmp_path):
    """Regression: a recipient with all-zero 16-bit chunks (e.g. a leading-zero / vanity address) MUST stay
    fully provable. Without the 0x10000 sentinel the zero chunk would be stored as a deletion and the lock's
    BIS would be permanently unredeemable."""
    pytest.importorskip("lmdb")
    import vm_state
    st = vm_state.VMState(str(tmp_path / "vault_zero"))
    try:
        vault = H(0x5A19)
        st.deploy(vault, bridge_vault.build())
        # chunks 0 and 1 are 0x0000; chunk 9 (last 2 bytes) is also exercised
        eth_recipient = bytes.fromhex("00000000aabbccddeeff00112233445566778899")
        assert _chunk(eth_recipient, 0) == SENTINEL and _chunk(eth_recipient, 1) == SENTINEL
        vm_engine._call(st, vault + ":" + eth_recipient.hex(), "sig", "f" * 56, vm_engine.VM_SINK, 555, 1)

        root = st.merkle_root()
        for j in range(10):                                              # including the zero chunks 0 and 1
            rec = st.merkle_prove_storage(vault, 17 + j)
            assert rec is not None, f"chunk {j} unprovable (stranded!)"
            assert rec["value"] == _chunk(eth_recipient, j)
            assert vm_merkle.verify_proof(bytes.fromhex(root), rec["leaf"], rec["proof"])
        rebuilt = b"".join((st.merkle_prove_storage(vault, 17 + j)["value"] & 0xFFFF).to_bytes(2, "big")
                           for j in range(10))
        assert rebuilt == eth_recipient
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
        # lock 1 amount at slot 1*16 = 16, lock 2 amount at slot 2*16 = 32
        a1 = st.merkle_prove_storage(vault, 16)
        a2 = st.merkle_prove_storage(vault, 32)
        assert a1["value"] == 700 and a2["value"] == 300
        root = st.merkle_root()
        assert a1["root"] == root and a2["root"] == root
        assert vm_merkle.verify_proof(bytes.fromhex(root), a1["leaf"], a1["proof"])
        assert vm_merkle.verify_proof(bytes.fromhex(root), a2["leaf"], a2["proof"])
    finally:
        st.close()
