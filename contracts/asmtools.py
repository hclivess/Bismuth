"""
asmtools.py — a thin label-resolving layer over the in-tree RV32I encoders (bismuth_riscv.py), used to
hand-author the demo contracts in this directory and their regnet tests.

This is TOOLING, exactly like the encoder helpers in bismuth_riscv (which carry the note "tests /
tooling, NOT consensus"): nodes execute the assembled *bytecode*, never this module. It exists only
because the base RV32I interpreter is plain machine code with no labels — writing branch/jump offsets by
hand is error-prone, so `Asm` resolves them in a two-pass assemble().

What it adds over the bare encoders:
  * named labels:           a.label("loop"); a.bne(rs1, rs2, "loop")
  * a full 32-bit load:     a.li(rd, 0xDEADBEEF)            (lui+addi with sign-extension carry)
  * the RV32I base has NO multiply/divide (no M-extension in this interpreter), so two gas-bounded
    software routines are provided as macros: mulu(rd, ra, rb) and divu(rq, rr, ra, rb). Both are
    shift-based and run in <= 32 iterations — well inside the 1,000,000-gas per-call budget. Contracts
    that need pro-rata arithmetic (the prediction market's payout) use these.

Register ABI convention used by the contracts (standard RISC-V names):
  zero=x0  ra=x1  sp=x2  a0..a7 = x10..x17  t0..t6 = x5,x6,x7,x28,x29,x30,x31  s0..s2 = x8,x9,x18
All contracts receive a0 = calldata pointer, a1 = calldata length (the engine's tiny ABI), and issue
host calls through a7 (the ECALL syscall number).
"""
import bismuth_riscv as rv

# register name -> number
ZERO, RA, SP = 0, 1, 2
A0, A1, A2, A3, A4, A5, A6, A7 = 10, 11, 12, 13, 14, 15, 16, 17
T0, T1, T2, T3, T4, T5, T6 = 5, 6, 7, 28, 29, 30, 31
S0, S1, S2, S3 = 8, 9, 18, 19

# syscall numbers (re-exported from the engine so contracts read clean)
SYS_HALT = rv.SYS_HALT
SYS_RETURN = rv.SYS_RETURN
SYS_SSTORE = rv.SYS_SSTORE
SYS_SLOAD = rv.SYS_SLOAD
SYS_CALLER = rv.SYS_CALLER
SYS_CALLVALUE = rv.SYS_CALLVALUE
SYS_NUMBER = rv.SYS_NUMBER
SYS_SHA256 = rv.SYS_SHA256
SYS_TRANSFER = rv.SYS_TRANSFER
SYS_CALL = rv.SYS_CALL                  # contract-to-contract call (doc/19 — issue #384)
SYS_DELEGATECALL = rv.SYS_DELEGATECALL  # run another contract's code in OUR storage/custody (proxy upgrade)
SYS_SETCODE = rv.SYS_SETCODE            # replace this contract's OWN code (in-place mutability)
SYS_SELFDESTRUCT = rv.SYS_SELFDESTRUCT  # pay out remaining custody + delete code/storage
SYS_ADDRESS = rv.SYS_ADDRESS            # write this contract's own 28-byte address
SYS_CALLER_FULL = rv.SYS_CALLER_FULL    # write the FULL 28-byte caller (collision-resistant identity)
SYS_KECCAK256 = rv.SYS_KECCAK256        # Ethereum keccak-256 (doc/45 bridge) — verify ETH headers/receipts
SYS_ECRECOVER = rv.SYS_ECRECOVER        # secp256k1 ecrecover -> 20-byte ETH address (doc/45 bridge)

# Branch mnemonic -> the (encoder, kind) it resolves to. "b" entries take a label and get a PC-relative
# offset computed in assemble(); everything else is absolute and emitted directly.
_BRANCHES = {
    "beq": rv.beq, "bne": rv.bne, "blt": rv.blt, "bge": rv.bge,
    "bltu": rv.bltu, "bgeu": rv.bgeu,
}

# Inverted branch condition, for relaxing an out-of-range conditional branch into "b<inv> over; jal target".
_INV_BRANCH = {"beq": "bne", "bne": "beq", "blt": "bge", "bge": "blt", "bltu": "bgeu", "bgeu": "bltu"}

# RV32I B-type immediate is a SIGNED 13-bit byte offset: reachable range [-4096, +4094]. A conditional
# branch to a farther label must be relaxed (jal reaches +-1 MiB).
_B_MIN, _B_MAX = -4096, 4094


class Asm:
    """A simple two-pass assembler. Emit instructions and labels in order; assemble() resolves every
    label-referencing branch/jump to a PC-relative offset and returns the bytecode."""

    def __init__(self):
        self._items = []  # ("ins", word) | ("label", name) | ("b", mnemonic, rs1, rs2, label) | ("jal", rd, label)

    # --- structure --------------------------------------------------------
    def label(self, name):
        self._items.append(("label", name))
        return self

    def raw(self, word):
        self._items.append(("ins", int(word) & 0xFFFFFFFF))
        return self

    def emit(self, *words):
        for w in words:
            self.raw(w)
        return self

    # --- ALU / immediates -------------------------------------------------
    def addi(self, rd, rs1, imm): return self.raw(rv.addi(rd, rs1, imm))
    def andi(self, rd, rs1, imm): return self.raw(rv.andi(rd, rs1, imm))
    def ori(self, rd, rs1, imm):  return self.raw(rv.ori(rd, rs1, imm))
    def xori(self, rd, rs1, imm): return self.raw(rv.xori(rd, rs1, imm))
    def slli(self, rd, rs1, sh):  return self.raw(rv.slli(rd, rs1, sh))
    def srli(self, rd, rs1, sh):  return self.raw(rv.srli(rd, rs1, sh))
    def add(self, rd, a, b):      return self.raw(rv.add(rd, a, b))
    def sub(self, rd, a, b):      return self.raw(rv.sub(rd, a, b))
    def and_(self, rd, a, b):     return self.raw(rv.and_(rd, a, b))
    def or_(self, rd, a, b):      return self.raw(rv.or_(rd, a, b))
    def sltu(self, rd, a, b):     return self.raw(rv.sltu(rd, a, b))
    def lui(self, rd, imm20):     return self.raw(rv.lui(rd, imm20))

    def mv(self, rd, rs):         return self.addi(rd, rs, 0)

    def li(self, rd, imm):
        for w in rv.li(rd, imm):
            self.raw(w)
        return self

    # --- memory -----------------------------------------------------------
    def lw(self, rd, rs1, imm):   return self.raw(rv.lw(rd, rs1, imm))
    def lbu(self, rd, rs1, imm):  return self.raw(rv.lbu(rd, rs1, imm))
    def sw(self, rs2, rs1, imm):  return self.raw(rv.sw(rs2, rs1, imm))
    def sb(self, rs2, rs1, imm):  return self.raw(rv.sb(rs2, rs1, imm))

    # --- control flow -----------------------------------------------------
    def _branch(self, mnem, rs1, rs2, label):
        self._items.append(("b", mnem, rs1, rs2, label))
        return self

    def beq(self, rs1, rs2, label):  return self._branch("beq", rs1, rs2, label)
    def bne(self, rs1, rs2, label):  return self._branch("bne", rs1, rs2, label)
    def blt(self, rs1, rs2, label):  return self._branch("blt", rs1, rs2, label)
    def bge(self, rs1, rs2, label):  return self._branch("bge", rs1, rs2, label)
    def bltu(self, rs1, rs2, label): return self._branch("bltu", rs1, rs2, label)
    def bgeu(self, rs1, rs2, label): return self._branch("bgeu", rs1, rs2, label)
    def j(self, label):
        self._items.append(("jal", ZERO, label))
        return self

    # --- ECALL helpers ----------------------------------------------------
    def syscall(self, num):
        return self.addi(A7, ZERO, num).raw(rv.ecall())

    def halt(self):              return self.syscall(SYS_HALT)
    def ret(self):               return self.syscall(SYS_RETURN)   # returns a0 (4 bytes, big-endian)
    def sstore(self, kr, vr):    return self.mv(A0, kr).mv(A1, vr).syscall(SYS_SSTORE)
    def sload_to(self, dr, kr):  return self.mv(A0, kr).syscall(SYS_SLOAD).mv(dr, A0)
    def caller(self, dr):        return self.syscall(SYS_CALLER).mv(dr, A0)
    def callvalue(self, dr):     return self.syscall(SYS_CALLVALUE).mv(dr, A0)
    def number(self, dr):        return self.syscall(SYS_NUMBER).mv(dr, A0)

    def transfer(self, addr_ptr_reg, amt_ptr_reg):
        """SYS_TRANSFER(a0=ptr to 28-byte recipient, a1=ptr to 8-byte amount) -> a0 = 1 if queued."""
        return self.mv(A0, addr_ptr_reg).mv(A1, amt_ptr_reg).syscall(SYS_TRANSFER)

    # --- contract-flexibility ECALLs (doc/19 — issue #384) ----------------
    def call(self, callee_ptr, value_ptr, cd_ptr, cd_len, ret_ptr, ret_len):
        """SYS_CALL: run the contract at [callee_ptr] (28-byte addr) in ITS own storage/custody, forwarding
        the 8-byte value at [value_ptr], passing cd_len bytes of calldata at [cd_ptr], copying up to ret_len
        bytes of the callee's RETURN word into [ret_ptr]. a0 = 1 on success else 0. Args are registers."""
        return (self.mv(A0, callee_ptr).mv(A1, value_ptr).mv(A2, cd_ptr).mv(A3, cd_len)
                .mv(A4, ret_ptr).mv(A5, ret_len).syscall(SYS_CALL))

    def delegatecall(self, impl_ptr, cd_ptr, cd_len, ret_ptr, ret_len):
        """SYS_DELEGATECALL: run the impl at [impl_ptr] (28-byte addr) in OUR storage/custody/identity
        (CALLER + CALLVALUE preserved, no value moves). a0 = 1 on success else 0. Args are registers."""
        return (self.mv(A0, impl_ptr).mv(A1, cd_ptr).mv(A2, cd_len)
                .mv(A3, ret_ptr).mv(A4, ret_len).syscall(SYS_DELEGATECALL))

    def setcode(self, code_ptr, code_len):
        """SYS_SETCODE: replace THIS contract's own code with code_len bytes at [code_ptr] (effective next
        call). a0 = 1 on success else 0. Gate it with your own owner check. Args are registers."""
        return self.mv(A0, code_ptr).mv(A1, code_len).syscall(SYS_SETCODE)

    def selfdestruct(self, benef_ptr_reg):
        """SYS_SELFDESTRUCT: pay remaining custody to the 28-byte beneficiary at [benef_ptr] and delete this
        contract's code + storage; halts (success). Arg is a register."""
        return self.mv(A0, benef_ptr_reg).syscall(SYS_SELFDESTRUCT)

    def address(self, out_ptr_reg):
        """SYS_ADDRESS: write this contract's own 28-byte address to [out_ptr]; a0 = its 32-bit fold."""
        return self.mv(A0, out_ptr_reg).syscall(SYS_ADDRESS)

    def caller_full(self, out_ptr_reg):
        """SYS_CALLER_FULL: write the full 28-byte caller address to [out_ptr] (collision-resistant identity
        for ownership/admin checks; SYS_CALLER only gives the grindable low 32 bits)."""
        return self.mv(A0, out_ptr_reg).syscall(SYS_CALLER_FULL)

    def store_u64_be(self, val_reg, base_reg, off=0, scratch=A6):
        """Serialize the 32-bit `val_reg` as the low word of an 8-byte BIG-ENDIAN field at [base_reg+off].
        SYS_TRANSFER reads the amount big-endian over 8 bytes, but registers are 32-bit, so the high 4
        bytes are 0 and the value occupies bytes 4..7 (MSB first). Clobbers `scratch`."""
        for i in range(4):                       # high 4 bytes = 0
            self.sb(ZERO, base_reg, off + i)
        for i in range(4):                       # low 4 bytes = val, MSB at off+4
            self.srli(scratch, val_reg, (3 - i) * 8)
            self.sb(scratch, base_reg, off + 4 + i)
        return self

    # --- arithmetic macros (RV32I has no MUL/DIV) -------------------------
    def mulu(self, rd, ra, rb, tmp_shift=T5, tmp_mcand=T6):
        """rd = (ra * rb) mod 2^32, unsigned. Shift-and-add over rb's bits; <= 32 iterations. rd must
        differ from ra/rb. Clobbers tmp_shift, tmp_mcand."""
        assert len({rd, ra, rb, tmp_shift, tmp_mcand}) == 5, "mulu needs distinct registers"
        loop = self._uniq("mul_loop")
        skip = self._uniq("mul_skip")
        done = self._uniq("mul_done")
        self.addi(rd, ZERO, 0)                  # acc = 0
        self.mv(tmp_mcand, ra)                   # mcand = ra (shifted left each step)
        self.mv(tmp_shift, rb)                    # bits = rb (shifted right each step)
        self.label(loop)
        self.beq(tmp_shift, ZERO, done)          # no bits left -> done
        self.andi(A6, tmp_shift, 1)              # (A6 scratch) low bit set?
        self.beq(A6, ZERO, skip)
        self.add(rd, rd, tmp_mcand)              # acc += mcand
        self.label(skip)
        self.slli(tmp_mcand, tmp_mcand, 1)       # mcand <<= 1
        self.srli(tmp_shift, tmp_shift, 1)       # bits >>= 1
        self.j(loop)
        self.label(done)
        return self

    def divu(self, rq, rr, ra, rb, tmp_i=T5, tmp_bit=T6):
        """rq = ra / rb, rr = ra % rb (unsigned, 32-bit), via restoring shift-subtract over 32 bits.
        If rb == 0, rq = 0 and rr = ra (defensive; callers must guard rb != 0 anyway). rq, rr, ra, rb,
        tmp_i, tmp_bit must be 6 distinct registers; uses A6 as inner scratch."""
        assert len({rq, rr, ra, rb, tmp_i, tmp_bit}) == 6, "divu needs 6 distinct registers"
        loop = self._uniq("div_loop")
        noset = self._uniq("div_noset")
        done = self._uniq("div_done")
        self.addi(rq, ZERO, 0)                   # quotient = 0
        self.addi(rr, ZERO, 0)                   # remainder = 0
        self.beq(rb, ZERO, done)                  # divide-by-zero guard -> q=0, r=0
        self.addi(tmp_i, ZERO, 31)               # i = 31 .. 0
        self.label(loop)
        self.slli(rr, rr, 1)                     # rem <<= 1
        self.srl_bit(tmp_bit, ra, tmp_i)         # bit = (ra >> i) & 1
        self.or_(rr, rr, tmp_bit)                 # rem |= bit
        self.sltu(A6, rr, rb)                     # A6 = (rem < rb)
        self.bne(A6, ZERO, noset)                # rem < rb -> this quotient bit stays 0
        self.sub(rr, rr, rb)                     # rem -= rb
        self.set_bit(rq, rq, tmp_i)              # quotient |= (1 << i)
        self.label(noset)
        self.beq(tmp_i, ZERO, done)              # i == 0 -> finished (avoid i going negative)
        self.addi(tmp_i, tmp_i, -1)
        self.j(loop)
        self.label(done)
        return self

    def srl_bit(self, rd, rs, ri):
        """rd = (rs >> ri) & 1, with ri a register (variable shift) — uses srl (R-type f3=5)."""
        self.raw((rs << 15) | (ri << 20) | (0x5 << 12) | (rd << 7) | 0x33)  # srl rd, rs, ri
        return self.andi(rd, rd, 1)

    def set_bit(self, rd, rs, ri):
        """rd = rs | (1 << ri), ri a register. Uses sll (R-type f3=1) on a 1."""
        self.addi(A6, ZERO, 1)
        self.raw((A6 << 15) | (ri << 20) | (0x1 << 12) | (A6 << 7) | 0x33)  # sll A6, A6, ri
        return self.or_(rd, rs, A6)

    def sll_r(self, rd, rs, rsh):
        """rd = rs << (rsh & 31), variable shift (R-type sll, f3=1)."""
        return self.raw((rsh << 20) | (rs << 15) | (0x1 << 12) | (rd << 7) | 0x33)

    def srl_r(self, rd, rs, rsh):
        """rd = (unsigned rs) >> (rsh & 31), variable shift (R-type srl, f3=5)."""
        return self.raw((rsh << 20) | (rs << 15) | (0x5 << 12) | (rd << 7) | 0x33)

    # --- 64-bit arithmetic (the 32-bit VM overflows on real-unit products) ---
    # The base ALU is 32-bit, so a product like stake*total (each ~1e8 units) overflows. These macros
    # carry a 64-bit value across a (hi, lo) register pair so the parimutuel payout is computed exactly.
    def mulu64(self, rhi, rlo, ra, rb, m_hi=T2, m_lo=T3, bits=T4, carry=A5):
        """(rhi:rlo) = ra * rb as a full 64-bit unsigned product (32x32 -> 64). Shift-and-add over rb's
        bits; <= 32 iterations. The 8 registers rhi,rlo,ra,rb,m_hi,m_lo,bits,carry must be DISTINCT; uses
        A6 as inner scratch. ra/rb are unchanged only if they don't alias the scratch set."""
        regs = {rhi, rlo, ra, rb, m_hi, m_lo, bits, carry}
        assert len(regs) == 8, "mulu64 needs 8 distinct registers"
        loop = self._uniq("mul64_loop"); skip = self._uniq("mul64_skip"); done = self._uniq("mul64_done")
        self.addi(rhi, ZERO, 0); self.addi(rlo, ZERO, 0)     # acc = 0
        self.mv(m_lo, ra); self.addi(m_hi, ZERO, 0)          # mcand = ra (64-bit, shifted left each step)
        self.mv(bits, rb)                                     # remaining multiplier bits
        self.label(loop)
        self.beq(bits, ZERO, done)
        self.andi(A6, bits, 1)
        self.beq(A6, ZERO, skip)
        # acc += mcand (64-bit): lo += m_lo with carry into hi; hi += m_hi
        self.add(rlo, rlo, m_lo)
        self.sltu(carry, rlo, m_lo)                           # carry = (new lo < addend) -> wrapped
        self.add(rhi, rhi, m_hi)
        self.add(rhi, rhi, carry)
        self.label(skip)
        # mcand <<= 1 (64-bit): new m_hi = (m_hi<<1) | (m_lo>>31); m_lo <<= 1
        self.srli(A6, m_lo, 31)
        self.slli(m_hi, m_hi, 1); self.or_(m_hi, m_hi, A6)
        self.slli(m_lo, m_lo, 1)
        self.srli(bits, bits, 1)
        self.j(loop)
        self.label(done)
        return self

    def divu64(self, rq, a_hi, a_lo, rb, rem=T5, bit=T6, i=A5):
        """rq = (a_hi:a_lo) / rb, an unsigned 64-bit / 32-bit divide whose 32-bit QUOTIENT is returned in
        rq (callers must ensure the true quotient fits 32 bits, as a parimutuel payout always does:
        payout = stake*total/winpot <= total < 2^32). Restoring shift-subtract over 64 bits; the running
        remainder stays < rb so it fits 32 bits. Only the low 32 quotient bits are kept (the high 32 are
        guaranteed 0 by the caller's range). rq,a_hi,a_lo,rb,rem,bit,i must be 7 distinct registers; uses
        A6 as inner scratch. rb must be != 0 (guard upstream)."""
        regs = {rq, a_hi, a_lo, rb, rem, bit, i}
        assert len(regs) == 7, "divu64 needs 7 distinct registers"
        loop = self._uniq("div64_loop"); lo_half = self._uniq("div64_lo")
        join = self._uniq("div64_join"); noset = self._uniq("div64_noset"); done = self._uniq("div64_done")
        self.addi(rq, ZERO, 0)                               # quotient (low 32 bits) = 0
        self.addi(rem, ZERO, 0)                              # remainder = 0
        self.beq(rb, ZERO, done)                              # divide-by-zero guard
        self.addi(i, ZERO, 63)                               # i = 63 .. 0 over the 64-bit dividend
        self.label(loop)
        # bit = (dividend >> i) & 1 — pick the half: i>=32 uses a_hi>>(i-32), else a_lo>>i
        self.addi(A6, ZERO, 32)
        self.bltu(i, A6, lo_half)
        self.sub(A6, i, A6)                                  # A6 = i - 32
        self.srl_r(bit, a_hi, A6)
        self.j(join)
        self.label(lo_half)
        self.srl_r(bit, a_lo, i)
        self.label(join)
        self.andi(bit, bit, 1)
        # capture the bit that (rem << 1) would shift out of 32 bits BEFORE shifting; if set, the true
        # 33-bit remainder is >= 2^32 > rb, so we must subtract regardless of the 32-bit compare.
        self.srli(A6, rem, 31)                               # A6 = overflow bit of the upcoming shift
        self.slli(rem, rem, 1)                               # rem <<= 1 (32-bit; top bit captured in A6)
        self.or_(rem, rem, bit)                              # rem |= dividend bit
        self.bne(A6, ZERO, "_div64_sub$%s" % join)           # overflow -> definitely subtract
        self.sltu(A6, rem, rb)                               # else A6 = (rem < rb)
        self.bne(A6, ZERO, noset)                            # rem < rb -> quotient bit 0
        self.label("_div64_sub$%s" % join)
        self.sub(rem, rem, rb)                               # rem -= rb
        # set quotient bit i (only the low 32 bits matter): if i < 32, rq |= (1 << i)
        self.addi(A6, ZERO, 32)
        self.bgeu(i, A6, noset)                              # i >= 32 -> bit is above the kept 32; skip
        self.set_bit(rq, rq, i)
        self.label(noset)
        self.beq(i, ZERO, done)                             # i == 0 -> finished (avoid going negative)
        self.addi(i, i, -1)
        self.j(loop)
        self.label(done)
        return self

    # --- assembly ---------------------------------------------------------
    _ctr = 0

    def _uniq(self, base):
        Asm._ctr += 1
        return "%s$%d" % (base, Asm._ctr)

    def assemble(self):
        # A conditional branch reaches only +-4 KiB (B-type), so in a large contract a branch to a far label
        # silently encodes the wrong (wrapped) offset. We relax any out-of-range "b" into an inverted branch
        # over a jal ("b<inv> +8; jal target"), which reaches +-1 MiB. Relaxing a branch grows the code by one
        # word, which can push OTHER branches out of range, so we iterate to a fixpoint (relaxation only ever
        # adds, so it converges). In-range contracts relax nothing and are byte-identical to before.
        def _addr_map(relaxed):
            addr = 0
            addrs = []                       # addrs[i] = byte address of item i
            labels = {}
            for i, it in enumerate(self._items):
                addrs.append(addr)
                if it[0] == "label":
                    labels[it[1]] = addr
                else:
                    addr += 8 if (it[0] == "b" and i in relaxed) else 4
            return addrs, labels

        relaxed = set()
        while True:
            addrs, labels = _addr_map(relaxed)
            changed = False
            for i, it in enumerate(self._items):
                if it[0] == "b" and i not in relaxed:
                    lbl = it[4]
                    if lbl not in labels:
                        raise KeyError("undefined label %r" % lbl)
                    off = labels[lbl] - addrs[i]
                    if not (_B_MIN <= off <= _B_MAX):
                        relaxed.add(i); changed = True
            if not changed:
                break

        words = []
        for i, it in enumerate(self._items):
            kind = it[0]
            if kind == "label":
                continue
            here = addrs[i]
            if kind == "ins":
                words.append(it[1])
            elif kind == "b":
                _, mnem, rs1, rs2, lbl = it
                tgt = labels[lbl]
                if i in relaxed:
                    words.append(_BRANCHES[_INV_BRANCH[mnem]](rs1, rs2, 8))   # skip the jal if NOT taken
                    words.append(rv.jal(ZERO, tgt - (here + 4)))              # taken: far jump to target
                else:
                    words.append(_BRANCHES[mnem](rs1, rs2, tgt - here))
            elif kind == "jal":
                _, rd, lbl = it
                if lbl not in labels:
                    raise KeyError("undefined label %r" % lbl)
                words.append(rv.jal(rd, labels[lbl] - here))
        return b"".join(int(w).to_bytes(4, "little") for w in words)
