"""
prediction_market.py — a Polymarket-style BINARY (YES/NO) prediction market, as one hand-authored RV32I
contract for the Bismuth decentralized-apps VM (bismuth_riscv / vm_engine).

WHAT IT DOES
  A single market on a yes/no question. Users back an outcome by depositing BIS into the contract; a
  single designated RESOLVER settles the result; winners then redeem their stake for a PRO-RATA share of
  the whole pot (winning + losing side). It is the canonical custody demo: deposits credit the contract's
  VM custody balance, payouts leave via SYS_TRANSFER (settled as consensus negative-height ledger rows).

  Parimutuel payout: a winner's payout = stake * (yes_pot + no_pot) / winning_pot. RV32I (this VM) has no
  hardware multiply/divide, so the contract computes this with the software mulu/divu macros in asmtools
  (both shift-based, <= 32 iterations — trivially inside the per-call gas budget).

DEPLOY
  operation = "vm:deploy", openfield = build(resolver_id).hex()
    resolver_id = low 32 bits of the resolver's Bismuth address int (the VM truncates CALLER to 32 bits,
    so the resolver is identified by that fold — baked into the bytecode as an immediate, like the HTLC's
    committed hash). Use resolver_id_of(address) to compute it.

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>")
  Each call's first 4 bytes (big-endian) are the function selector:
    FN_BUY_YES (0)  — send BIS to VM_SINK with this call; the attached value buys YES shares.
    FN_BUY_NO  (1)  — likewise for NO.
    FN_RESOLVE (2)  — calldata = selector(4) | outcome(4 BE), outcome 1=YES, 2=NO.
                      Reverts unless caller == resolver AND market not already resolved AND outcome in
                      {1,2}.  (no double-resolve; resolver-only.)
    FN_CLAIM   (3)  — calldata = selector(4) | recipient(28 BE). Pays the caller's pro-rata winnings to
                      `recipient`. Reverts unless resolved, caller has winning-side stake, and has not
                      already claimed.  (claim-once; losers / non-stakers get nothing.)
  A revert (failed require) commits NO storage and NO transfer — the attached deposit, if any, stays in
  custody (callers must not attach value to RESOLVE/CLAIM).

STORAGE (32-bit word slots)
  1 = resolved flag (0/1)              3 = yes_pot (units)
  2 = winning outcome (1=YES,2=NO)     4 = no_pot  (units)
  0x1xxxxxxx = per-user YES stake   (key = 0x10000000 | (caller & 0x0FFFFFFF))
  0x2xxxxxxx = per-user NO  stake   (key = 0x20000000 | (caller & 0x0FFFFFFF))
  0x3xxxxxxx = per-user claimed flag(key = 0x30000000 | (caller & 0x0FFFFFFF))

NOTE / LIMITS (32-bit VM): pots and per-user stakes are 32-bit unsigned units (1 BIS = 1e8 units), so a
single market here works comfortably up to a few BIS of total pot in tests. A production market would
want the 64-bit-aware storage a real higher-level toolchain (C/Rust -> RV32I, see report) would bring.
"""
import hashlib

from asmtools import Asm, A0, A2, A3, A4, A5, S0, S1, S2, S3, T0, T1, T2, T3, T4, ZERO

# selectors
FN_BUY_YES = 0
FN_BUY_NO = 1
FN_RESOLVE = 2
FN_CLAIM = 3

# storage slots
S_RESOLVED = 1
S_OUTCOME = 2
S_YESPOT = 3
S_NOPOT = 4
TAG_YES = 0x10000000
TAG_NO = 0x20000000
TAG_CLAIMED = 0x30000000
USER_MASK = 0x0FFFFFFF

SCRATCH = 4096          # scratch memory for the 8-byte transfer amount (past code + calldata)


def resolver_id_of(address):
    """Low 32 bits of a Bismuth address int — how the VM (which truncates CALLER to 32 bits) sees it."""
    try:
        return int(str(address), 16) & 0xFFFFFFFF
    except Exception:
        return int.from_bytes(hashlib.blake2b(str(address).encode(), digest_size=32).digest(),
                              "big") & 0xFFFFFFFF


def _load_be32(a, rd, base_reg, off):
    """rd = 32-bit big-endian word at [base_reg+off] (calldata is laid out big-endian by callers)."""
    a.lbu(rd, base_reg, off)
    a.slli(rd, rd, 8)
    a.lbu(A2, base_reg, off + 1); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8)
    a.lbu(A2, base_reg, off + 2); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8)
    a.lbu(A2, base_reg, off + 3); a.or_(rd, rd, A2)


def _add_slot(a, key_reg, amt_reg):
    """storage[key] += amt  (key in key_reg, amount in amt_reg). Uses A4 as scratch."""
    a.sload_to(A4, key_reg)
    a.add(A4, A4, amt_reg)
    a.sstore(key_reg, A4)


def build(resolver_id):
    """Assemble the market bytecode with `resolver_id` (32-bit) baked in as the only authorised settler."""
    a = Asm()

    # S0 holds the calldata pointer for the whole call: the syscall ABI clobbers a0, so we stash a0 in a
    # callee-stable register up front and read all calldata through S0.
    # T0 = selector, T1 = caller fold, T2 = caller & USER_MASK (per-user slot suffix)
    a.mv(S0, A0)
    _load_be32(a, T0, S0, 0)
    a.caller(T1)
    a.li(A5, USER_MASK)
    a.and_(T2, T1, A5)

    a.li(A2, FN_BUY_YES); a.beq(T0, A2, "buy_yes")
    a.li(A2, FN_BUY_NO);  a.beq(T0, A2, "buy_no")
    a.li(A2, FN_RESOLVE); a.beq(T0, A2, "resolve")
    a.li(A2, FN_CLAIM);   a.beq(T0, A2, "claim")
    a.j("revert")

    # --- BUY_YES: require value>0; yes_pot += v; user_yes += v ------------
    a.label("buy_yes")
    a.callvalue(T3)
    a.beq(T3, ZERO, "revert")
    a.li(A3, S_YESPOT); _add_slot(a, A3, T3)
    a.li(A3, TAG_YES); a.or_(A3, A3, T2); _add_slot(a, A3, T3)
    a.halt()

    # --- BUY_NO ----------------------------------------------------------
    a.label("buy_no")
    a.callvalue(T3)
    a.beq(T3, ZERO, "revert")
    a.li(A3, S_NOPOT); _add_slot(a, A3, T3)
    a.li(A3, TAG_NO); a.or_(A3, A3, T2); _add_slot(a, A3, T3)
    a.halt()

    # --- RESOLVE: resolver-only, once, outcome in {1,2} ------------------
    a.label("resolve")
    a.li(A2, resolver_id)
    a.bne(T1, A2, "revert")
    a.li(A3, S_RESOLVED); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")
    _load_be32(a, T3, S0, 4)
    a.li(A2, 1); a.beq(T3, A2, "outcome_ok")
    a.li(A2, 2); a.beq(T3, A2, "outcome_ok")
    a.j("revert")
    a.label("outcome_ok")
    a.li(A3, S_OUTCOME); a.sstore(A3, T3)
    a.li(A3, S_RESOLVED); a.li(A4, 1); a.sstore(A3, A4)
    a.halt()

    # --- CLAIM: pay caller's pro-rata winnings to calldata recipient -----
    a.label("claim")
    a.li(A3, S_RESOLVED); a.sload_to(A4, A3); a.beq(A4, ZERO, "revert")
    a.li(A3, TAG_CLAIMED); a.or_(A3, A3, T2); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")

    # winning-side stake -> T4, winning pot -> A5
    a.li(A3, S_OUTCOME); a.sload_to(T3, A3)
    a.li(A2, 1); a.beq(T3, A2, "win_yes")
    a.li(A3, TAG_NO); a.or_(A3, A3, T2); a.sload_to(T4, A3)
    a.li(A3, S_NOPOT); a.sload_to(A5, A3)
    a.j("have_stake")
    a.label("win_yes")
    a.li(A3, TAG_YES); a.or_(A3, A3, T2); a.sload_to(T4, A3)
    a.li(A3, S_YESPOT); a.sload_to(A5, A3)
    a.label("have_stake")
    a.beq(T4, ZERO, "revert")                     # no winning stake -> nothing to claim

    # total = yes_pot + no_pot ; payout = stake*total/winpot.
    # stake and total are ~1e8-unit values, so stake*total overflows 32 bits — compute the product in a
    # full 64-bit (hi:lo) pair, then do a 64/32 divide. The payout <= total < 2^32, so it fits a 32-bit
    # word for SSTORE/TRANSFER. (T2 = user suffix and S0 = calldata ptr are preserved throughout.)
    a.li(A3, S_YESPOT); a.sload_to(A2, A3)
    a.li(A3, S_NOPOT);  a.sload_to(A3, A3)
    a.add(A2, A2, A3)                              # A2 = total pot
    a.mulu64(T0, T1, T4, A2, m_hi=T3, m_lo=S1, bits=S2, carry=S3)   # (T0:T1) = stake*total (64-bit)
    a.divu64(A4, T0, T1, A5, rem=S1, bit=S2, i=S3)                  # A4 = payout = product / winpot
    a.beq(A4, ZERO, "mark_claimed")

    a.li(A3, SCRATCH)
    a.store_u64_be(A4, A3, 0)                       # 8-byte big-endian amount at SCRATCH
    a.mv(A4, A3)                                    # A4 = amount ptr
    a.addi(A3, S0, 4)                              # A3 = recipient ptr = calldata+4 (28 bytes)
    a.transfer(A3, A4)                             # SYS_TRANSFER(recipient, amount)

    a.label("mark_claimed")
    a.li(A3, TAG_CLAIMED); a.or_(A3, A3, T2); a.li(A4, 1); a.sstore(A3, A4)
    a.halt()

    # --- revert: an illegal instruction is a deterministic revert (no commit, no transfer) ---
    a.label("revert")
    a.raw(0)

    return a.assemble()
