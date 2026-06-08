"""
Deterministic VM core (bismuth_vm) — unit tests.

The properties that MUST hold for a chain VM: byte-identical determinism (two nodes agree) and
gas-bounded halting (no infinite-loop / DoS). These lock both, plus the safety rails (modular overflow,
validated jumps, revert-on-fault).

Run with: python3 -m pytest tests/test_vm.py -v
"""
import hashlib

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


def test_sha256_matches_hashlib():
    secret = 0xC0FFEE
    code = push(secret, 32) + bytes([vm.SHA256, vm.RETURN])
    r = execute(code)
    expected = int.from_bytes(hashlib.sha256(secret.to_bytes(32, "big")).digest(), "big")
    assert r.success and int.from_bytes(r.output, "big") == expected


def test_number_is_block_height():
    r = execute(bytes([vm.NUMBER, vm.RETURN]), block_height=4846123)
    assert r.success and int.from_bytes(r.output, "big") == 4846123


def test_htlc_preimage_claim():
    # The HTLC claim path as VM bytecode: reveal secret S in calldata; the contract accepts iff
    # sha256(S) == the committed hash H (storage slot 0). This is exactly BIP-199's hash-preimage path.
    # CALLDATALOAD 0 -> S ; SHA256 ; SLOAD 0 -> H ; EQ ; RETURN
    secret = 0xDEADBEEF
    H = int.from_bytes(hashlib.sha256(secret.to_bytes(32, "big")).digest(), "big")
    code = push(0) + bytes([vm.CALLDATALOAD, vm.SHA256]) + push(0) + bytes([vm.SLOAD, vm.EQ, vm.RETURN])

    ok = execute(code, calldata=secret.to_bytes(32, "big"), storage={0: H})       # correct preimage
    assert ok.success and int.from_bytes(ok.output, "big") == 1
    bad = execute(code, calldata=(secret + 1).to_bytes(32, "big"), storage={0: H})  # wrong preimage
    assert bad.success and int.from_bytes(bad.output, "big") == 0


def test_transfer_is_balance_checked():
    # value custody (VM level): a contract may queue a BIS transfer only up to its balance.
    # TRANSFER pops amount then to, so the stack is [to, amount].
    ok = execute(push(0xABC) + push(30) + bytes([vm.TRANSFER, vm.RETURN]), self_balance=100)
    assert ok.success and int.from_bytes(ok.output, "big") == 1     # affordable -> queued
    assert ok.transfers == [(0xABC, 30)]
    short = execute(push(0xABC) + push(200) + bytes([vm.TRANSFER, vm.RETURN]), self_balance=100)
    assert short.success and int.from_bytes(short.output, "big") == 0   # too much -> refused
    assert short.transfers == []                                    # nothing queued, no overspend


def test_htlc_claim_with_timelock():
    # The FULL HTLC claim guard: accept iff (correct preimage) AND (still within the timeout window).
    # preimage_ok = sha256(calldata) == H[slot0]; within = NOT(height > timeout[slot1]); return AND.
    secret = 0xABCDEF
    H = int.from_bytes(hashlib.sha256(secret.to_bytes(32, "big")).digest(), "big")
    timeout = 1000
    code = (push(0) + bytes([vm.CALLDATALOAD, vm.SHA256]) + push(0) + bytes([vm.SLOAD, vm.EQ]) +
            push(1) + bytes([vm.SLOAD, vm.NUMBER, vm.GT, vm.ISZERO, vm.AND, vm.RETURN]))
    storage = {0: H, 1: timeout}
    good = secret.to_bytes(32, "big")

    # correct preimage, BEFORE timeout -> claim valid
    assert int.from_bytes(execute(code, calldata=good, storage=storage, block_height=900).output, "big") == 1
    # correct preimage, AFTER timeout -> refused: the TIMELOCK fired (this is the refund window now)
    assert int.from_bytes(execute(code, calldata=good, storage=storage, block_height=1100).output, "big") == 0
    # wrong preimage, before timeout -> refused
    bad = (secret + 1).to_bytes(32, "big")
    assert int.from_bytes(execute(code, calldata=bad, storage=storage, block_height=900).output, "big") == 0
