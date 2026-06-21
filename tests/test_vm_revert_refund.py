"""
VM value-custody: a REVERTING value-bearing call must REFUND the sender (H2).

The bug: a vm:call carrying BIS to VM_SINK credited the contract's custody balance BEFORE execution; on a
REVERT the storage was rolled back but the custody credit was kept, so the attached value was stranded —
no storage slot owned it, and supply/custody drifted. Fix: on revert (or any fault) undo the custody
credit and queue a refund payout back to the sender, so supply stays exact and the sink keeps mirroring
the sum of contract balances.

These are pure unit tests of vm_engine against the real (lmdb) state store — no live node.

Run with: python3 -m pytest tests/test_vm_revert_refund.py -v
"""
import pytest

pytest.importorskip("lmdb")

import bismuth_riscv as rv
from bismuth_riscv import asm, addi, ecall

import vm_engine
import vm_state

# A contract that HALTs cleanly (success) and a contract that faults (illegal instruction -> revert).
OK_CODE = asm(addi(17, 0, rv.SYS_HALT), ecall())
REVERT_CODE = b"\xff\xff\xff\xff"            # illegal instruction -> deterministic revert (success=False)

SENDER = "11" * 28                            # a 56-hex Bismuth-style address


def _sink_supply_invariant(state, contracts):
    """The sink's ledger balance MUST equal the sum of all contract custody balances (double-entry).
    We model the sink balance as (deposits credited to the sink ledger by the funding txs) minus
    (payouts settled FROM the sink). Here we just assert custody == what the engine says it holds."""
    return sum(state.get_balance(c) for c in contracts)


def test_revert_refunds_sender_and_keeps_supply_exact(tmp_path):
    state = vm_state.VMState(str(tmp_path / "s"))
    try:
        rev = vm_engine.contract_address("sig-revert")
        state.deploy(rev, REVERT_CODE)
        assert _sink_supply_invariant(state, [rev]) == 0

        # a value-bearing call (5 units sent to the sink, targeting `rev`) that REVERTS
        deposit = 5
        payouts = vm_engine._call(
            state, openfield=rev + ":", signature="sig-call", sender=SENDER,
            recipient=vm_engine.VM_SINK, amount_units=deposit, block_height=1)

        # the sender is refunded the full attached value
        assert payouts == [(SENDER, deposit)], payouts
        # custody is NOT left credited -> nothing stranded; the sink still mirrors the (zero) custody sum
        assert state.get_balance(rev) == 0, "deposit stranded in custody on revert"
        assert _sink_supply_invariant(state, [rev]) == 0
    finally:
        state.close()


def test_revert_without_value_refunds_nothing(tmp_path):
    state = vm_state.VMState(str(tmp_path / "s"))
    try:
        rev = vm_engine.contract_address("sig-revert2")
        state.deploy(rev, REVERT_CODE)
        # a zero-value reverting call queues no payout (nothing to refund) and strands nothing
        payouts = vm_engine._call(
            state, openfield=rev + ":", signature="sig", sender=SENDER,
            recipient=vm_engine.VM_SINK, amount_units=0, block_height=1)
        assert payouts == []
        assert state.get_balance(rev) == 0
    finally:
        state.close()


def test_call_to_missing_contract_refunds_value(tmp_path):
    state = vm_state.VMState(str(tmp_path / "s"))
    try:
        # value sent to the sink targeting a contract that does not exist -> refund (it would otherwise be
        # stranded in the sink with no custody to back it)
        payouts = vm_engine._call(
            state, openfield=("ab" * 28) + ":", signature="sig", sender=SENDER,
            recipient=vm_engine.VM_SINK, amount_units=7, block_height=1)
        assert payouts == [(SENDER, 7)], payouts
    finally:
        state.close()


def test_successful_value_call_keeps_custody(tmp_path):
    state = vm_state.VMState(str(tmp_path / "s"))
    try:
        ok = vm_engine.contract_address("sig-ok")
        state.deploy(ok, OK_CODE)
        deposit = 9
        payouts = vm_engine._call(
            state, openfield=ok + ":", signature="sig-call", sender=SENDER,
            recipient=vm_engine.VM_SINK, amount_units=deposit, block_height=1)
        # success: no refund, the deposit STAYS in custody (the contract now owns it)
        assert payouts == []
        assert state.get_balance(ok) == deposit
        assert _sink_supply_invariant(state, [ok]) == deposit
    finally:
        state.close()


def test_deploy_address_is_content_derived_not_signature(tmp_path):
    """doc/18 §A.1 'final decision' + audit: the contract address must derive from the deploy tx's CONTENT
    txid, not its (post-fork malleable) signature — so a deployer cannot grind the signature to move the
    address. Two deploys with identical content but different signatures must land at the SAME address."""
    code = OK_CODE.hex()
    # 12-field row: (bh, ts, addr, recip, amount, sig, pub, hash, fee, reward, op, openfield)
    base = (5, "0", SENDER, SENDER, 0, None, "pub", "hash", "0", "0", "vm:deploy", code)
    row_a = base[:5] + ("sig-AAAA",) + base[6:]
    row_b = base[:5] + ("sig-BBBB-totally-different",) + base[6:]   # different signature, same content
    addr = vm_engine.contract_address(vm_engine._tx_id_of(row_a))

    s1 = vm_state.VMState(str(tmp_path / "s1"))
    try:
        vm_engine.apply_block_rows(s1, [row_a])
        assert s1.get_code(addr) is not None, "deploy did not land at the content-txid address"
        # and NOT at the old signature-derived address (the malleability hole that was closed)
        assert s1.get_code(vm_engine.contract_address("sig-AAAA")) is None
    finally:
        s1.close()

    s2 = vm_state.VMState(str(tmp_path / "s2"))
    try:
        vm_engine.apply_block_rows(s2, [row_b])
        assert s2.get_code(addr) is not None, "same content + different sig must give the SAME address"
    finally:
        s2.close()


def test_apply_block_rows_refund_payout_settles_against_deposit(tmp_path):
    """End-to-end at the row level: a reverting value call produces exactly the refund payout the digester
    settles from the sink, netting the sender to zero against the funding tx (supply exact)."""
    state = vm_state.VMState(str(tmp_path / "s"))
    try:
        rev = vm_engine.contract_address("sig-r")
        state.deploy(rev, REVERT_CODE)
        # transactions-row layout: (bh, ts, addr, recip, amount, sig, pub, hash, fee, reward, op, openfield)
        deposit = 4
        row = (1, "0", SENDER, vm_engine.VM_SINK, deposit, "sig-call", "pub",
               "hash", "0", "0", "vm:call", rev + ":")
        payouts = vm_engine.apply_block_rows(state, [row])
        # the only payout is the refund; settling it (sink -> sender, `deposit`) cancels the funding tx
        # (sender -> sink, `deposit`), so the sender nets 0 and the sink nets 0 — supply unchanged.
        assert payouts == [(SENDER, deposit)], payouts
        assert state.get_balance(rev) == 0
    finally:
        state.close()
