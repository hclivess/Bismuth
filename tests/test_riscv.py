"""
Deterministic RV32I RISC-V interpreter (bismuth_riscv) — unit tests.

Same properties the bytecode engine must have: byte-identical determinism and gas-bounded halting,
plus the integer ALU, branches, memory, and the ECALL host ABI (storage / return).

Run with: python3 -m pytest tests/test_riscv.py -v
"""
import hashlib

import bismuth_riscv as rv
from bismuth_riscv import execute, asm, addi, add, sub, bne, lw, lui, ecall

A0, A1, A2, A3, A4, A7 = 10, 11, 12, 13, 14, 17


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


def test_transfer_is_balance_checked():
    # value custody: TRANSFER(a0=ptr 28-byte recipient, a1=ptr 8-byte amount). calldata = recipient|amount.
    # a0 already points at the calldata (the recipient); set a1=a0+28 (the amount) and ecall; return a0.
    code = asm(addi(A1, A0, 28), addi(A7, 0, rv.SYS_TRANSFER), ecall(),
               addi(A7, 0, rv.SYS_RETURN), ecall())
    recipient = (0xABC).to_bytes(28, "big")
    ok = execute(code, calldata=recipient + (30).to_bytes(8, "big"), self_balance=100)
    assert ok.success and int.from_bytes(ok.output, "big") == 1     # affordable -> queued
    assert ok.transfers == [(0xABC, 30)]
    short = execute(code, calldata=recipient + (200).to_bytes(8, "big"), self_balance=100)
    assert short.success and int.from_bytes(short.output, "big") == 0   # over balance -> refused
    assert short.transfers == []                                    # no overspend


def test_sha256_via_ecall():
    # the HTLC primitive: SHA256(a0=ptr,a1=len,a2=out) hashes the calldata; return its first word
    data = b"bismuth-htlc-secret-padded-to32!"          # 32 bytes
    code = asm(addi(A2, 0, 2000), addi(A7, 0, rv.SYS_SHA256), ecall(),
               lw(A0, A2, 0), addi(A7, 0, rv.SYS_RETURN), ecall())
    r = execute(code, calldata=data)
    expected = int.from_bytes(hashlib.sha256(data).digest()[0:4], "little")
    assert r.success and int.from_bytes(r.output, "big") == expected


def test_htlc_preimage_releases_value():
    # The flagship atomic-swap claim (BIP-199), as one RV32I contract: the locked value is released to the
    # recipient ONLY if sha256(secret) matches the committed hash H. Combines SHA256 + a compare + a value
    # TRANSFER. calldata = secret(32) | recipient(28) | amount(8).
    secret = b"the-htlc-secret-padded-to-32byte"   # 32 bytes
    H = int.from_bytes(hashlib.sha256(secret).digest()[0:4], "little")
    lo, hi = H & 0xFFF, (H >> 12) & 0xFFFFF
    if lo >= 0x800:                                  # addi sign-extends the low 12 bits -> carry into hi
        lo -= 0x1000
        hi = (hi + 1) & 0xFFFFF
    code = asm(
        addi(A1, 0, 32), addi(A2, 0, 2000), addi(A7, 0, rv.SYS_SHA256), ecall(),  # sha256(secret)->mem[2000]
        lw(A3, A2, 0),                                   # a3 = hash first word
        lui(A4, hi), addi(A4, A4, lo),                  # a4 = committed H
        bne(A3, A4, 20),                                # preimage mismatch -> skip the transfer (to HALT)
        addi(A0, A0, 32), addi(A1, A0, 28),             # a0=recipient ptr (cd+32), a1=amount ptr (cd+60)
        addi(A7, 0, rv.SYS_TRANSFER), ecall(),
        addi(A7, 0, rv.SYS_HALT), ecall())
    recipient = (0xBEEF).to_bytes(28, "big")
    ok = execute(code, calldata=secret + recipient + (50).to_bytes(8, "big"), self_balance=100)
    assert ok.success and ok.transfers == [(0xBEEF, 50)]    # correct preimage -> value released
    no = execute(code, calldata=(b"x" * 32) + recipient + (50).to_bytes(8, "big"), self_balance=100)
    assert no.success and no.transfers == []                # wrong preimage -> nothing released
