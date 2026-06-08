"""
bismuth_riscv.py — a deterministic RV32I (base integer) RISC-V interpreter: an ALTERNATIVE execution
engine for the decentralized-apps VM (doc/19), the direction Ethereum is also taking (Vitalik's
"RISC-V over the EVM"). RISC-V is a real, frozen, widely-implemented ISA with mature toolchains —
you compile C/Rust/etc. straight to it — and its base integer set is inherently deterministic.

Same consensus guarantees as the bytecode engine (bismuth_vm):
  * integer-only 32-bit two's-complement (no floats);
  * a pure function of (code, calldata, context, storage) — no clock / randomness / host state;
  * GAS metering per instruction (runaway programs halt, never hang — and gas bounds the step count);
  * bounded memory; the guest stays sandboxed, interacting with the chain ONLY through ECALL.

This is the ENGINE only. The vm_state store, digest fork-gate, rollback rebuild and state root are all
engine-agnostic, so they would drive this identically to the bytecode VM.

ECALL ABI (syscall number in a7/x17, args in a0.., return in a0):
  0 HALT · 1 RETURN(a0) · 2 SSTORE(a0=key,a1=val) · 3 SLOAD(a0=key)->a0 ·
  4 CALLER->a0 · 5 CALLVALUE->a0 · 6 NUMBER(block height)->a0 · 7 SHA256(a0=ptr,a1=len,a2=out_ptr)
"""
import hashlib

WMASK = 0xFFFFFFFF
SYS_HALT, SYS_RETURN, SYS_SSTORE, SYS_SLOAD, SYS_CALLER, SYS_CALLVALUE, SYS_NUMBER, SYS_SHA256 = range(8)


class RiscVError(Exception):
    pass


def _sext(value, bits):
    """Sign-extend a bits-wide value to a Python int."""
    sign = 1 << (bits - 1)
    return (value & (sign - 1)) - (value & sign)


class Result:
    __slots__ = ("success", "output", "gas_used", "storage")

    def __init__(self, success, output, gas_used, storage):
        self.success = success
        self.output = output
        self.gas_used = gas_used
        self.storage = storage


def execute(code, calldata=b"", caller=0, callvalue=0, storage=None, gas_limit=1_000_000,
            block_height=0, mem_size=1 << 16):
    """Run RV32I `code` deterministically. Code is loaded at address 0; calldata right after, with
    a0 = calldata pointer and a1 = calldata length (a tiny ABI). Returns a Result (commit storage only
    if success). Any fault is a deterministic revert, never a leaked Python exception."""
    storage = dict(storage or {})
    reg = [0] * 32
    mem = bytearray(mem_size)
    if len(code) > mem_size:
        return Result(False, b"", 0, storage)
    mem[0:len(code)] = code
    cd_at = (len(code) + 3) & ~3
    if cd_at + len(calldata) <= mem_size:
        mem[cd_at:cd_at + len(calldata)] = calldata
    reg[10] = cd_at & WMASK          # a0 = calldata ptr
    reg[11] = len(calldata) & WMASK  # a1 = calldata len
    pc = 0
    gas = int(gas_limit)

    def load(addr, n, signed):
        if addr < 0 or addr + n > mem_size:
            raise RiscVError("bad load")
        v = int.from_bytes(mem[addr:addr + n], "little")
        return _sext(v, n * 8) & WMASK if signed else v

    def store(addr, n, val):
        if addr < 0 or addr + n > mem_size:
            raise RiscVError("bad store")
        mem[addr:addr + n] = (val & ((1 << (n * 8)) - 1)).to_bytes(n, "little")

    try:
        while True:
            if gas < 1:
                return Result(False, b"", gas_limit, storage)        # out of gas -> revert
            if pc < 0 or pc + 4 > mem_size:
                return Result(False, b"", gas_limit - gas, storage)
            inst = int.from_bytes(mem[pc:pc + 4], "little")
            gas -= 1
            op = inst & 0x7F
            rd = (inst >> 7) & 0x1F
            f3 = (inst >> 12) & 0x7
            rs1 = (inst >> 15) & 0x1F
            rs2 = (inst >> 20) & 0x1F
            f7 = (inst >> 25) & 0x7F
            npc = (pc + 4) & WMASK

            if op == 0x33:                                           # R-type
                a, b = reg[rs1], reg[rs2]
                if f3 == 0:   r = (a - b) if f7 == 0x20 else (a + b)
                elif f3 == 1: r = a << (b & 31)
                elif f3 == 2: r = 1 if _sext(a, 32) < _sext(b, 32) else 0
                elif f3 == 3: r = 1 if (a & WMASK) < (b & WMASK) else 0
                elif f3 == 4: r = a ^ b
                elif f3 == 5: r = (_sext(a, 32) >> (b & 31)) if f7 == 0x20 else ((a & WMASK) >> (b & 31))
                elif f3 == 6: r = a | b
                else:         r = a & b
                reg[rd] = r & WMASK
            elif op == 0x13:                                         # I-type ALU
                imm = _sext(inst >> 20, 12)
                a = reg[rs1]
                if f3 == 0:   r = a + imm
                elif f3 == 1: r = a << ((inst >> 20) & 31)
                elif f3 == 2: r = 1 if _sext(a, 32) < imm else 0
                elif f3 == 3: r = 1 if (a & WMASK) < (imm & WMASK) else 0
                elif f3 == 4: r = a ^ imm
                elif f3 == 5:
                    sh = (inst >> 20) & 31
                    r = (_sext(a, 32) >> sh) if f7 == 0x20 else ((a & WMASK) >> sh)
                elif f3 == 6: r = a | imm
                else:         r = a & imm
                reg[rd] = r & WMASK
            elif op == 0x03:                                         # loads
                addr = (reg[rs1] + _sext(inst >> 20, 12)) & WMASK
                if f3 == 0:   reg[rd] = load(addr, 1, True)
                elif f3 == 1: reg[rd] = load(addr, 2, True)
                elif f3 == 2: reg[rd] = load(addr, 4, True)
                elif f3 == 4: reg[rd] = load(addr, 1, False)
                elif f3 == 5: reg[rd] = load(addr, 2, False)
                else:         return Result(False, b"", gas_limit - gas, storage)
            elif op == 0x23:                                         # stores (S-type imm)
                imm = _sext(((f7 << 5) | rd), 12)
                addr = (reg[rs1] + imm) & WMASK
                if f3 == 0:   store(addr, 1, reg[rs2])
                elif f3 == 1: store(addr, 2, reg[rs2])
                elif f3 == 2: store(addr, 4, reg[rs2])
                else:         return Result(False, b"", gas_limit - gas, storage)
            elif op == 0x63:                                         # branches (B-type imm)
                imm = _sext((((inst >> 31) & 1) << 12) | (((inst >> 7) & 1) << 11) |
                            (((inst >> 25) & 0x3F) << 5) | (((inst >> 8) & 0xF) << 1), 13)
                a, b = reg[rs1], reg[rs2]
                if f3 == 0:   take = a == b
                elif f3 == 1: take = a != b
                elif f3 == 4: take = _sext(a, 32) < _sext(b, 32)
                elif f3 == 5: take = _sext(a, 32) >= _sext(b, 32)
                elif f3 == 6: take = (a & WMASK) < (b & WMASK)
                elif f3 == 7: take = (a & WMASK) >= (b & WMASK)
                else:         return Result(False, b"", gas_limit - gas, storage)
                if take:
                    npc = (pc + imm) & WMASK
            elif op == 0x37:                                         # LUI
                reg[rd] = (inst & 0xFFFFF000) & WMASK
            elif op == 0x17:                                         # AUIPC
                reg[rd] = (pc + (inst & 0xFFFFF000)) & WMASK
            elif op == 0x6F:                                         # JAL (J-type imm)
                imm = _sext((((inst >> 31) & 1) << 20) | (((inst >> 12) & 0xFF) << 12) |
                            (((inst >> 20) & 1) << 11) | (((inst >> 21) & 0x3FF) << 1), 21)
                reg[rd] = npc & WMASK
                npc = (pc + imm) & WMASK
            elif op == 0x67:                                         # JALR
                t = npc
                npc = ((reg[rs1] + _sext(inst >> 20, 12)) & ~1) & WMASK
                reg[rd] = t & WMASK
            elif op == 0x73:                                         # SYSTEM
                if (inst >> 20) == 0:                                # ECALL
                    s = reg[17]                                      # a7
                    if s == SYS_HALT:
                        return Result(True, b"", gas_limit - gas, storage)
                    elif s == SYS_RETURN:
                        return Result(True, (reg[10] & WMASK).to_bytes(4, "big"), gas_limit - gas, storage)
                    elif s == SYS_SSTORE:
                        storage[reg[10] & WMASK] = reg[11] & WMASK
                    elif s == SYS_SLOAD:
                        reg[10] = storage.get(reg[10] & WMASK, 0) & WMASK
                    elif s == SYS_CALLER:
                        reg[10] = caller & WMASK
                    elif s == SYS_CALLVALUE:
                        reg[10] = callvalue & WMASK
                    elif s == SYS_NUMBER:
                        reg[10] = block_height & WMASK
                    elif s == SYS_SHA256:
                        ptr, ln, out = reg[10] & WMASK, reg[11] & WMASK, reg[12] & WMASK
                        if ptr + ln > mem_size or out + 32 > mem_size:
                            return Result(False, b"", gas_limit - gas, storage)
                        gas -= 60
                        mem[out:out + 32] = hashlib.sha256(bytes(mem[ptr:ptr + ln])).digest()
                    else:
                        return Result(False, b"", gas_limit - gas, storage)  # unknown syscall
                else:                                               # EBREAK -> halt
                    return Result(True, b"", gas_limit - gas, storage)
            else:
                return Result(False, b"", gas_limit - gas, storage)  # illegal instruction

            reg[0] = 0                                               # x0 is hardwired zero
            pc = npc
    except RiscVError:
        return Result(False, b"", gas_limit - gas, storage)


# --- tiny assembler (tests / tooling, NOT consensus) -----------------------------------------------
def addi(rd, rs1, imm):  return (((imm & 0xFFF) << 20) | (rs1 << 15) | (rd << 7) | 0x13)
def add(rd, rs1, rs2):   return ((rs2 << 20) | (rs1 << 15) | (rd << 7) | 0x33)
def sub(rd, rs1, rs2):   return ((0x20 << 25) | (rs2 << 20) | (rs1 << 15) | (rd << 7) | 0x33)
def beq(rs1, rs2, imm):  return _b_type(rs1, rs2, imm, 0)
def bne(rs1, rs2, imm):  return _b_type(rs1, rs2, imm, 1)
def ecall():             return 0x00000073


def _b_type(rs1, rs2, imm, f3):
    i = imm & 0x1FFF
    return ((((i >> 12) & 1) << 31) | (((i >> 5) & 0x3F) << 25) | (rs2 << 20) | (rs1 << 15) |
            (f3 << 12) | (((i >> 1) & 0xF) << 8) | (((i >> 11) & 1) << 7) | 0x63)


def asm(*insts):
    return b"".join(int(i).to_bytes(4, "little") for i in insts)
