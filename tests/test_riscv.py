"""
Deterministic RV32I RISC-V interpreter (bismuth_riscv) — unit tests.

Same properties the bytecode engine must have: byte-identical determinism and gas-bounded halting,
plus the integer ALU, branches, memory, and the ECALL host ABI (storage / return).

Run with: python3 -m pytest tests/test_riscv.py -v
"""
import bismuth_riscv as rv
from bismuth_riscv import execute, asm, addi, add, sub, bne, ecall

A0, A1, A7 = 10, 11, 17


def test_add_and_return():
    code = asm(addi(A0, 0, 2), addi(A1, 0, 3), add(A0, A0, A1),
               addi(A7, 0, rv.SYS_RETURN), ecall())
    r = execute(code)
    assert r.success and int.from_bytes(r.output, "big") == 5


def test_sub_is_signed_correct():
    code = asm(addi(A0, 0, 10), addi(A1, 0, 7), sub(A0, A0, A1),
               addi(A7, 0, rv.SYS_RETURN), ecall())
    r = execute(code)
    assert r.success and int.from_bytes(r.output, "big") == 3


def test_storage_via_ecall_roundtrip():
    code = asm(addi(A0, 0, 7), addi(A1, 0, 42), addi(A7, 0, rv.SYS_SSTORE), ecall(),  # store[7]=42
               addi(A0, 0, 7), addi(A7, 0, rv.SYS_SLOAD), ecall(),                    # a0 = store[7]
               addi(A7, 0, rv.SYS_RETURN), ecall())
    r = execute(code)
    assert r.success and int.from_bytes(r.output, "big") == 42
    assert r.storage.get(7) == 42


def test_determinism():
    code = asm(addi(A0, 0, 9), addi(A1, 0, 4), add(A0, A0, A1),
               addi(A7, 0, rv.SYS_RETURN), ecall())
    a, b = execute(code, gas_limit=9999), execute(code, gas_limit=9999)
    assert a.success and a.output == b.output and a.gas_used == b.gas_used


def test_branch_taken():
    # a0=5, a1=6 -> bne taken -> a0=111 ; (not taken would be 222)
    code = asm(addi(A0, 0, 5), addi(A1, 0, 6), bne(A0, A1, 16),
               addi(A0, 0, 222), addi(A7, 0, rv.SYS_RETURN), ecall(),
               addi(A0, 0, 111), addi(A7, 0, rv.SYS_RETURN), ecall())
    r = execute(code)
    assert r.success and int.from_bytes(r.output, "big") == 111


def test_infinite_loop_halts_on_gas():
    code = asm(0x0000006F)                 # jal x0, 0 -> jumps to itself forever
    r = execute(code, gas_limit=1000)
    assert not r.success and r.gas_used == 1000
