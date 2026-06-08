"""
Deterministic VM core (bismuth_vm) — unit tests.

The properties that MUST hold for a chain VM: byte-identical determinism (two nodes agree) and
gas-bounded halting (no infinite-loop / DoS). These lock both, plus the safety rails (modular overflow,
validated jumps, revert-on-fault).

Run with: python3 -m pytest tests/test_vm.py -v
"""
import bismuth_vm as vm
from bismuth_vm import execute, push


def test_arithmetic_and_return():
    code = push(2) + push(3) + bytes([vm.ADD, vm.RETURN])     # 2 + 3
    r = execute(code)
    assert r.success and int.from_bytes(r.output, "big") == 5


def test_modular_overflow_is_deterministic():
    code = push(vm.MASK, 32) + push(1) + bytes([vm.ADD, vm.RETURN])   # (2^256-1) + 1 wraps to 0
    r = execute(code)
    assert r.success and int.from_bytes(r.output, "big") == 0


def test_same_inputs_same_outputs():
    code = push(7) + push(6) + bytes([vm.MUL]) + push(5) + bytes([vm.ADD, vm.RETURN])
    a = execute(code, gas_limit=10_000)
    b = execute(code, gas_limit=10_000)
    assert a.success and b.success
    assert a.output == b.output and a.gas_used == b.gas_used   # deterministic to the gas unit


def test_storage_roundtrip_committed_only_on_success():
    # SSTORE storage[1]=42 ; SLOAD storage[1] ; RETURN
    code = push(42) + push(1) + bytes([vm.SSTORE]) + push(1) + bytes([vm.SLOAD, vm.RETURN])
    r = execute(code)
    assert r.success and int.from_bytes(r.output, "big") == 42
    assert r.storage == {1: 42}


def test_infinite_loop_halts_on_gas():
    # JUMPDEST ; PUSH 0 ; JUMP  -> spins forever; must run OUT OF GAS and return, not hang
    code = bytes([vm.JUMPDEST]) + push(0) + bytes([vm.JUMP])
    r = execute(code, gas_limit=1_000)
    assert not r.success and r.gas_used == 1_000           # all gas consumed, terminated


def test_invalid_jump_into_push_data_reverts():
    # dest 1 is inside PUSH's operand (the length byte), not a JUMPDEST -> deterministic revert
    code = push(1) + bytes([vm.JUMP])
    r = execute(code)
    assert not r.success


def test_revert_signals_no_commit():
    code = push(9) + push(1) + bytes([vm.SSTORE, vm.REVERT])
    r = execute(code)
    assert not r.success                                    # caller must discard r.storage


def test_conditional_branch_taken():
    # PUSH 1 (cond); PUSH dest; JUMPI; [fallthrough] PUSH 222 RETURN; JUMPDEST PUSH 111 RETURN
    head = push(1)                                   # cond = 1 (truthy -> take the jump)
    fall = push(222) + bytes([vm.RETURN])            # the cond-false fallthrough branch
    dest = len(head) + 4 + 1 + len(fall)             # head + PUSH(dest,2)=4 + JUMPI=1 + fall ; JUMPDEST here
    code = head + push(dest, 2) + bytes([vm.JUMPI]) + fall + bytes([vm.JUMPDEST]) + push(111) + bytes([vm.RETURN])
    r = execute(code)
    assert r.success and int.from_bytes(r.output, "big") == 111
