"""
bismuth_riscv.py — a deterministic RV32I (base integer) RISC-V interpreter: THE execution engine for the
decentralized-apps VM (doc/19), the direction Ethereum is also taking (Vitalik's "RISC-V over the EVM").
RISC-V is a real, frozen, widely-implemented ISA with mature toolchains — you compile C/Rust/etc. straight
to it — and its base integer set is inherently deterministic.

Consensus guarantees:
  * integer-only 32-bit two's-complement (no floats);
  * a pure function of (code, calldata, context, storage) — no clock / randomness / host state;
  * GAS metering per instruction (runaway programs halt, never hang — and gas bounds the step count);
  * bounded memory; the guest stays sandboxed, interacting with the chain ONLY through ECALL.

This is the ENGINE only. The vm_state store, digest fork-gate, rollback rebuild and state root drive it
through a single execute()/Result + `host` contract.

ECALL ABI (syscall number in a7/x17, args in a0.., return in a0):
  0 HALT · 1 RETURN(a0) · 2 SSTORE(a0=key,a1=val) · 3 SLOAD(a0=key)->a0 ·
  4 CALLER->a0 · 5 CALLVALUE->a0 · 6 NUMBER(block height)->a0 · 7 SHA256(a0=ptr,a1=len,a2=out_ptr) ·
  8 TRANSFER(a0=ptr to 28-byte recipient, a1=ptr to 8-byte amount)->a0=1 if affordable else 0

CONTRACT-FLEXIBILITY ops (doc/19 — issue #384; additive, post-fork-only with the rest of the VM):
  9  CALL(a0=ptr 28-byte callee, a1=ptr 8-byte value, a2=calldata ptr, a3=calldata len,
        a4=ret buf ptr, a5=ret buf len) -> a0 = 1 on success else 0.
        Runs the CALLEE's code in the callee's own storage + custody; forwards `value` (debited from the
        caller's custody, credited to the callee's); on success copies up to ret-len bytes of the callee's
        RETURN word into the ret buffer; on revert NOTHING the inner call did persists.
  10 DELEGATECALL(a0=ptr 28-byte impl, a1=calldata ptr, a2=calldata len, a3=ret buf ptr, a4=ret buf len)
        -> a0 = 1/0. Runs the IMPL's code in the CALLER's own storage + custody + identity (CALLER and
        CALLVALUE are preserved, NO value moves). The single primitive a stable proxy needs to swap logic.
  11 SETCODE(a0=ptr new code, a1=code len) -> a0 = 1/0. Replaces the EXECUTING contract's OWN code
        (in-place mutability / code replacement). Gated by whatever access check the contract itself runs.
        The new code takes effect on the NEXT call (the current frame keeps running the old code).
  12 SELFDESTRUCT(a0=ptr 28-byte beneficiary) -> halts (success); pays the contract's remaining custody to
        the beneficiary (as a TRANSFER-style payout) and deletes its code + storage.
  13 ADDRESS(a0=out ptr) -> writes the executing contract's own 28-byte address; also returns its 32-bit
        fold in a0 (the CALLER-style identity word).
  14 CALLER_FULL(a0=out ptr) -> writes the FULL 28-byte (224-bit) caller address to memory. SYS_CALLER (4)
        only exposes the low 32 bits, which is grindable (~2^32) for high-stakes access control; CALLER_FULL
        is the collision-resistant identity for ownership / admin checks (see contracts/ownable.py).

HOST: storage / custody / code all route through a `host` object (default: a one-contract in-memory host
built from `storage`/`self_balance`, so the legacy single-contract call is byte-identical). The chain
passes a journaled, MULTI-contract host (vm_engine.Host) keyed by the 56-hex contract `address`, which is
what makes CALL/DELEGATECALL re-entrant and atomically revertable. Determinism is unchanged: every effect
is still a pure function of (code, calldata, context, the host's committed state).
"""
import hashlib

WMASK = 0xFFFFFFFF
(SYS_HALT, SYS_RETURN, SYS_SSTORE, SYS_SLOAD, SYS_CALLER, SYS_CALLVALUE, SYS_NUMBER, SYS_SHA256,
 SYS_TRANSFER, SYS_CALL, SYS_DELEGATECALL, SYS_SETCODE, SYS_SELFDESTRUCT, SYS_ADDRESS,
 SYS_CALLER_FULL) = range(15)

# --- consensus constants for the contract-flexibility ops (priced once; every node agrees) -----------
MAX_CALL_DEPTH = 16            # hard cap on nested CALL/DELEGATECALL frames. Fires WELL before any Python
#                               recursion limit (so the limit is the consensus constant, never a platform
#                               RecursionError) and bounds the call-tree a single tx can build.
GAS_CALL = 100                 # base cost of a CALL / DELEGATECALL (the callee's own gas adds on top)
GAS_SETCODE = 50               # base cost of SETCODE; + 1 gas / byte of new code (bounds code-churn griefing)
GAS_SELFDESTRUCT = 50          # cost of SELFDESTRUCT
ADDR_MASK = (1 << 224) - 1     # 224-bit (28-byte / 56-hex) Bismuth address mask
_SELF = "self"                 # sentinel address of the lone contract on the hostless (single-contract) path


class RiscVError(Exception):
    pass


def _addr_int(addr):
    """Fold a 56-hex contract address to its integer value (for CALLER/ADDRESS words). The hostless
    sentinel (`_SELF`) and any non-hex value fold to 0 — that path never makes cross-contract calls."""
    try:
        return int(addr, 16)
    except (ValueError, TypeError):
        return 0


class _DictHost:
    """The default host for the hostless `execute()` path: ONE contract under the `_SELF` sentinel, held
    in memory. It keeps the observable Result (`storage`, `transfers`) byte-identical to the pre-host
    engine, so the unit tests and the off-chain ``Chain`` harness are unchanged. SSTORE/SLOAD/TRANSFER and
    the new SETCODE/SELFDESTRUCT/ADDRESS all work on `self`; a cross-contract CALL/DELEGATECALL finds no
    peer (``get_code`` of any other address is None) and fails cleanly — full multi-contract calls are
    driven by the journaled vm_engine.Host (or a test stand-in)."""

    def __init__(self, storage, self_balance):
        self._stor = dict(storage or {})
        self._bal = int(self_balance or 0)
        self.new_code = None          # set by SETCODE — observable so a single-contract test can assert it
        self.destroyed = False        # set by SELFDESTRUCT
        self.payouts = []             # [(to_int, amount)] external transfers / selfdestruct beneficiaries

    def get_code(self, addr):
        return None                   # no peers; the executing contract's own code is already in `mem`

    def sload(self, addr, key):
        return self._stor.get(key, 0)

    def sstore(self, addr, key, val):
        self._stor[key] = val

    def get_balance(self, addr):
        return self._bal

    def add_balance(self, addr, delta):
        self._bal = max(0, self._bal + int(delta))

    def set_code(self, addr, code):
        self.new_code = bytes(code)

    def destroy(self, addr):
        self.destroyed = True
        self._stor = {}
        self._bal = 0

    def add_payout(self, to, amount):
        self.payouts.append((to, amount))

    def contract_storage(self, addr):
        return self._stor

    def checkpoint(self):
        return (dict(self._stor), self._bal, self.new_code, self.destroyed, len(self.payouts))

    def revert(self, t):
        self._stor, self._bal, self.new_code, self.destroyed = dict(t[0]), t[1], t[2], t[3]
        del self.payouts[t[4]:]


def _sext(value, bits):
    """Sign-extend a bits-wide value to a Python int."""
    sign = 1 << (bits - 1)
    return (value & (sign - 1)) - (value & sign)


class Result:
    __slots__ = ("success", "output", "gas_used", "storage", "transfers")

    def __init__(self, success, output, gas_used, storage, transfers=None):
        self.success = success
        self.output = output
        self.gas_used = gas_used
        self.storage = storage
        self.transfers = transfers if transfers is not None else []  # [(to_int, amount)]; apply iff success


def execute(code, calldata=b"", caller=0, callvalue=0, storage=None, gas_limit=1_000_000,
            block_height=0, mem_size=1 << 16, self_balance=0, host=None, address=None, depth=0):
    """Run RV32I `code` deterministically. Code is loaded at address 0; calldata right after, with
    a0 = calldata pointer and a1 = calldata length (a tiny ABI). Returns a Result (commit storage only
    if success). Any fault is a deterministic revert, never a leaked Python exception.

    `host` mediates ALL storage / custody / code effects so the same engine runs a lone contract (the
    default in-memory host built from `storage`/`self_balance` — observably identical to before) AND a
    multi-contract call tree (the journaled host vm_engine passes, keyed by the 56-hex `address`).
    `depth` is the current nesting level (0 = top); CALL/DELEGATECALL recurse with depth+1, share the
    remaining gas, and take a host checkpoint so an inner revert rolls back only the inner frame."""
    if host is None:                       # hostless: one contract under the _SELF sentinel
        host = _DictHost(storage, self_balance)
        address = _SELF
    reg = [0] * 32
    mem = bytearray(mem_size)

    def _result(success, output, g):
        # storage/transfers are meaningful only for the TOP frame on the hostless path (the chain path
        # reads the host journal directly); they mirror this contract's slots + the external payouts so far.
        return Result(success, output, g, host.contract_storage(address), list(host.payouts))

    if len(code) > mem_size:
        return _result(False, b"", 0)
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
                return _result(False, b"", gas_limit)                # out of gas -> revert (full budget)
            if pc < 0 or pc + 4 > mem_size:
                return _result(False, b"", gas_limit - gas)
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
                else:         return _result(False, b"", gas_limit - gas)
            elif op == 0x23:                                         # stores (S-type imm)
                imm = _sext(((f7 << 5) | rd), 12)
                addr = (reg[rs1] + imm) & WMASK
                if f3 == 0:   store(addr, 1, reg[rs2])
                elif f3 == 1: store(addr, 2, reg[rs2])
                elif f3 == 2: store(addr, 4, reg[rs2])
                else:         return _result(False, b"", gas_limit - gas)
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
                else:         return _result(False, b"", gas_limit - gas)
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
                        return _result(True, b"", gas_limit - gas)
                    elif s == SYS_RETURN:
                        return _result(True, (reg[10] & WMASK).to_bytes(4, "big"), gas_limit - gas)
                    elif s == SYS_SSTORE:
                        host.sstore(address, reg[10] & WMASK, reg[11] & WMASK)
                    elif s == SYS_SLOAD:
                        reg[10] = host.sload(address, reg[10] & WMASK) & WMASK
                    elif s == SYS_CALLER:
                        reg[10] = caller & WMASK
                    elif s == SYS_CALLVALUE:
                        reg[10] = callvalue & WMASK
                    elif s == SYS_NUMBER:
                        reg[10] = block_height & WMASK
                    elif s == SYS_SHA256:
                        ptr, ln, out = reg[10] & WMASK, reg[11] & WMASK, reg[12] & WMASK
                        if ptr + ln > mem_size or out + 32 > mem_size:
                            return _result(False, b"", gas_limit - gas)
                        gas -= 60
                        mem[out:out + 32] = hashlib.sha256(bytes(mem[ptr:ptr + ln])).digest()
                    elif s == SYS_TRANSFER:
                        aptr, vptr = reg[10] & WMASK, reg[11] & WMASK   # a0=ptr 28-byte addr, a1=ptr 8-byte amount
                        if aptr + 28 > mem_size or vptr + 8 > mem_size:
                            return _result(False, b"", gas_limit - gas)
                        to = int.from_bytes(mem[aptr:aptr + 28], "big")    # full 224-bit recipient
                        amount = int.from_bytes(mem[vptr:vptr + 8], "big")  # 64-bit units (regs are 32-bit)
                        if 0 < amount <= host.get_balance(address):    # can't transfer more than held
                            host.add_balance(address, -amount)
                            host.add_payout(to, amount)
                            reg[10] = 1
                        else:
                            reg[10] = 0
                    elif s == SYS_CALL:
                        # a0=callee ptr(28), a1=value ptr(8), a2/a3=calldata ptr/len, a4/a5=ret buf ptr/len
                        cap, vp = reg[10] & WMASK, reg[11] & WMASK
                        cdp, cdl = reg[12] & WMASK, reg[13] & WMASK
                        rbp, rbl = reg[14] & WMASK, reg[15] & WMASK
                        if (cap + 28 > mem_size or vp + 8 > mem_size or cdp + cdl > mem_size
                                or rbp + rbl > mem_size):
                            return _result(False, b"", gas_limit - gas)
                        gas -= GAS_CALL
                        callee = "%056x" % (int.from_bytes(mem[cap:cap + 28], "big") & ADDR_MASK)
                        value = int.from_bytes(mem[vp:vp + 8], "big")
                        code2 = host.get_code(callee)
                        if (gas < 1 or depth >= MAX_CALL_DEPTH or code2 is None
                                or value > 0xFFFFFFFF or value > host.get_balance(address)):
                            reg[10] = 0                               # call fails; caller decides what to do
                        else:
                            sub_cd = bytes(mem[cdp:cdp + cdl])
                            cp = host.checkpoint()
                            if value:                                 # move custody caller -> callee
                                host.add_balance(address, -value)
                                host.add_balance(callee, value)
                            sub = execute(code2, calldata=sub_cd, caller=_addr_int(address),
                                          callvalue=value, gas_limit=gas, block_height=block_height,
                                          mem_size=mem_size, host=host, address=callee, depth=depth + 1)
                            gas -= sub.gas_used                       # the subtree's gas is charged to us
                            if sub.success:
                                n = min(rbl, len(sub.output))
                                if n:
                                    mem[rbp:rbp + n] = sub.output[:n]
                                reg[10] = 1
                            else:
                                host.revert(cp)                       # undo value move + every inner write
                                reg[10] = 0
                    elif s == SYS_DELEGATECALL:
                        # a0=impl ptr(28), a1/a2=calldata ptr/len, a3/a4=ret buf ptr/len. Borrowed code runs
                        # in OUR storage + custody + identity: address, caller and callvalue are preserved.
                        ip = reg[10] & WMASK
                        cdp, cdl = reg[11] & WMASK, reg[12] & WMASK
                        rbp, rbl = reg[13] & WMASK, reg[14] & WMASK
                        if ip + 28 > mem_size or cdp + cdl > mem_size or rbp + rbl > mem_size:
                            return _result(False, b"", gas_limit - gas)
                        gas -= GAS_CALL
                        impl = "%056x" % (int.from_bytes(mem[ip:ip + 28], "big") & ADDR_MASK)
                        code2 = host.get_code(impl)
                        if gas < 1 or depth >= MAX_CALL_DEPTH or code2 is None:
                            reg[10] = 0
                        else:
                            sub_cd = bytes(mem[cdp:cdp + cdl])
                            cp = host.checkpoint()
                            sub = execute(code2, calldata=sub_cd, caller=caller, callvalue=callvalue,
                                          gas_limit=gas, block_height=block_height, mem_size=mem_size,
                                          host=host, address=address, depth=depth + 1)
                            gas -= sub.gas_used
                            if sub.success:
                                n = min(rbl, len(sub.output))
                                if n:
                                    mem[rbp:rbp + n] = sub.output[:n]
                                reg[10] = 1
                            else:
                                host.revert(cp)
                                reg[10] = 0
                    elif s == SYS_SETCODE:
                        # a0=new code ptr, a1=len. Replaces the EXECUTING contract's OWN code; effective on
                        # the NEXT call (this frame keeps running the old code already loaded in `mem`).
                        cptr, clen = reg[10] & WMASK, reg[11] & WMASK
                        if cptr + clen > mem_size:
                            return _result(False, b"", gas_limit - gas)
                        gas -= GAS_SETCODE + clen
                        if clen == 0:
                            reg[10] = 0                               # empty code is rejected, not stored
                        else:
                            host.set_code(address, bytes(mem[cptr:cptr + clen]))
                            reg[10] = 1
                    elif s == SYS_SELFDESTRUCT:
                        # a0=beneficiary ptr(28). Pay the remaining custody to the beneficiary, delete code +
                        # storage, and HALT this frame with success.
                        bp = reg[10] & WMASK
                        if bp + 28 > mem_size:
                            return _result(False, b"", gas_limit - gas)
                        gas -= GAS_SELFDESTRUCT
                        benef = int.from_bytes(mem[bp:bp + 28], "big") & ADDR_MASK
                        bal = host.get_balance(address)
                        if bal > 0:
                            host.add_balance(address, -bal)
                            host.add_payout(benef, bal)
                        host.destroy(address)
                        return _result(True, b"", gas_limit - gas)
                    elif s == SYS_ADDRESS:
                        # a0=out ptr -> write our own 28-byte address; also return its 32-bit fold in a0.
                        optr = reg[10] & WMASK
                        if optr + 28 > mem_size:
                            return _result(False, b"", gas_limit - gas)
                        me = _addr_int(address) & ADDR_MASK
                        mem[optr:optr + 28] = me.to_bytes(28, "big")
                        reg[10] = me & WMASK
                    elif s == SYS_CALLER_FULL:
                        # a0=out ptr -> write the FULL 28-byte caller (collision-resistant identity for
                        # ownership; SYS_CALLER only exposes the grindable low 32 bits).
                        optr = reg[10] & WMASK
                        if optr + 28 > mem_size:
                            return _result(False, b"", gas_limit - gas)
                        mem[optr:optr + 28] = (caller & ADDR_MASK).to_bytes(28, "big")
                    else:
                        return _result(False, b"", gas_limit - gas)  # unknown syscall
                else:                                               # EBREAK -> halt
                    return _result(True, b"", gas_limit - gas)
            else:
                return _result(False, b"", gas_limit - gas)  # illegal instruction

            reg[0] = 0                                               # x0 is hardwired zero
            pc = npc
    except RiscVError:
        return _result(False, b"", gas_limit - gas)


# --- tiny assembler (tests / tooling, NOT consensus) -----------------------------------------------
# Pure instruction-WORD encoders: they only build bytecode that the consensus execute() above decodes;
# they never touch consensus state. The contracts in contracts/ (and asmtools' label layer) author RV32I
# through these. Every encoder here corresponds to a path execute() already implements (verified 1:1).

# I-type ALU (op 0x13): rd = rs1 OP imm
def addi(rd, rs1, imm):  return (((imm & 0xFFF) << 20) | (rs1 << 15) | (0x0 << 12) | (rd << 7) | 0x13)
def slti(rd, rs1, imm):  return (((imm & 0xFFF) << 20) | (rs1 << 15) | (0x2 << 12) | (rd << 7) | 0x13)
def sltiu(rd, rs1, imm): return (((imm & 0xFFF) << 20) | (rs1 << 15) | (0x3 << 12) | (rd << 7) | 0x13)
def xori(rd, rs1, imm):  return (((imm & 0xFFF) << 20) | (rs1 << 15) | (0x4 << 12) | (rd << 7) | 0x13)
def ori(rd, rs1, imm):   return (((imm & 0xFFF) << 20) | (rs1 << 15) | (0x6 << 12) | (rd << 7) | 0x13)
def andi(rd, rs1, imm):  return (((imm & 0xFFF) << 20) | (rs1 << 15) | (0x7 << 12) | (rd << 7) | 0x13)
def slli(rd, rs1, sh):   return (((sh & 0x1F) << 20) | (rs1 << 15) | (0x1 << 12) | (rd << 7) | 0x13)
def srli(rd, rs1, sh):   return (((sh & 0x1F) << 20) | (rs1 << 15) | (0x5 << 12) | (rd << 7) | 0x13)
def srai(rd, rs1, sh):   return ((0x20 << 25) | ((sh & 0x1F) << 20) | (rs1 << 15) | (0x5 << 12) | (rd << 7) | 0x13)

# R-type (op 0x33): rd = rs1 OP rs2
def add(rd, rs1, rs2):   return ((rs2 << 20) | (rs1 << 15) | (0x0 << 12) | (rd << 7) | 0x33)
def sub(rd, rs1, rs2):   return ((0x20 << 25) | (rs2 << 20) | (rs1 << 15) | (0x0 << 12) | (rd << 7) | 0x33)
def sll(rd, rs1, rs2):   return ((rs2 << 20) | (rs1 << 15) | (0x1 << 12) | (rd << 7) | 0x33)
def slt(rd, rs1, rs2):   return ((rs2 << 20) | (rs1 << 15) | (0x2 << 12) | (rd << 7) | 0x33)
def sltu(rd, rs1, rs2):  return ((rs2 << 20) | (rs1 << 15) | (0x3 << 12) | (rd << 7) | 0x33)
def xor_(rd, rs1, rs2):  return ((rs2 << 20) | (rs1 << 15) | (0x4 << 12) | (rd << 7) | 0x33)
def srl(rd, rs1, rs2):   return ((rs2 << 20) | (rs1 << 15) | (0x5 << 12) | (rd << 7) | 0x33)
def sra(rd, rs1, rs2):   return ((0x20 << 25) | (rs2 << 20) | (rs1 << 15) | (0x5 << 12) | (rd << 7) | 0x33)
def or_(rd, rs1, rs2):   return ((rs2 << 20) | (rs1 << 15) | (0x6 << 12) | (rd << 7) | 0x33)
def and_(rd, rs1, rs2):  return ((rs2 << 20) | (rs1 << 15) | (0x7 << 12) | (rd << 7) | 0x33)

# loads (op 0x03): rd = mem[rs1+imm]
def lb(rd, rs1, imm):    return (((imm & 0xFFF) << 20) | (rs1 << 15) | (0x0 << 12) | (rd << 7) | 0x03)
def lh(rd, rs1, imm):    return (((imm & 0xFFF) << 20) | (rs1 << 15) | (0x1 << 12) | (rd << 7) | 0x03)
def lw(rd, rs1, imm):    return (((imm & 0xFFF) << 20) | (rs1 << 15) | (0x2 << 12) | (rd << 7) | 0x03)
def lbu(rd, rs1, imm):   return (((imm & 0xFFF) << 20) | (rs1 << 15) | (0x4 << 12) | (rd << 7) | 0x03)
def lhu(rd, rs1, imm):   return (((imm & 0xFFF) << 20) | (rs1 << 15) | (0x5 << 12) | (rd << 7) | 0x03)

# stores (op 0x23, S-type imm): mem[rs1+imm] = rs2
def sb(rs2, rs1, imm):   return _s_type(rs1, rs2, imm, 0x0)
def sh(rs2, rs1, imm):   return _s_type(rs1, rs2, imm, 0x1)
def sw(rs2, rs1, imm):   return _s_type(rs1, rs2, imm, 0x2)

# branches (op 0x63, B-type imm): if (rs1 OP rs2) pc += imm
def beq(rs1, rs2, imm):  return _b_type(rs1, rs2, imm, 0)
def bne(rs1, rs2, imm):  return _b_type(rs1, rs2, imm, 1)
def blt(rs1, rs2, imm):  return _b_type(rs1, rs2, imm, 4)
def bge(rs1, rs2, imm):  return _b_type(rs1, rs2, imm, 5)
def bltu(rs1, rs2, imm): return _b_type(rs1, rs2, imm, 6)
def bgeu(rs1, rs2, imm): return _b_type(rs1, rs2, imm, 7)

# upper-immediate / jumps
def lui(rd, imm20):      return (((imm20 & 0xFFFFF) << 12) | (rd << 7) | 0x37)
def auipc(rd, imm20):    return (((imm20 & 0xFFFFF) << 12) | (rd << 7) | 0x17)
def jal(rd, imm):        return _j_type(rd, imm)
def jalr(rd, rs1, imm):  return (((imm & 0xFFF) << 20) | (rs1 << 15) | (0x0 << 12) | (rd << 7) | 0x67)
def ecall():             return 0x00000073


def li(rd, imm):
    """Pseudo-op: load a full 32-bit `imm` into rd as (lui, addi), accounting for addi's sign-extension
    of the low 12 bits (carry into the upper 20). Returns 1 or 2 instruction words."""
    imm &= WMASK
    lo = imm & 0xFFF
    hi = (imm >> 12) & 0xFFFFF
    if lo >= 0x800:                       # addi sign-extends bit 11 -> bump the upper part to compensate
        hi = (hi + 1) & 0xFFFFF
        lo -= 0x1000                      # negative low (two's-complement 12-bit)
    if hi == 0:                           # fits in a single addi from x0
        return [addi(rd, 0, lo)]
    if lo == 0:
        return [lui(rd, hi)]
    return [lui(rd, hi), addi(rd, rd, lo)]


def _s_type(rs1, rs2, imm, f3):
    i = imm & 0xFFF
    return (((i >> 5) << 25) | (rs2 << 20) | (rs1 << 15) | (f3 << 12) | ((i & 0x1F) << 7) | 0x23)


def _b_type(rs1, rs2, imm, f3):
    i = imm & 0x1FFF
    return ((((i >> 12) & 1) << 31) | (((i >> 5) & 0x3F) << 25) | (rs2 << 20) | (rs1 << 15) |
            (f3 << 12) | (((i >> 1) & 0xF) << 8) | (((i >> 11) & 1) << 7) | 0x63)


def _j_type(rd, imm):
    i = imm & 0x1FFFFF
    return ((((i >> 20) & 1) << 31) | (((i >> 1) & 0x3FF) << 21) | (((i >> 11) & 1) << 20) |
            (((i >> 12) & 0xFF) << 12) | (rd << 7) | 0x6F)


def asm(*insts):
    return b"".join(int(i).to_bytes(4, "little") for i in insts)
