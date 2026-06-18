"""
amm.py — an automated market maker (constant-product x*y=k) as one hand-authored RV32I contract for the
Bismuth in-core VM (bismuth_riscv / vm_engine). This is the documented follow-up to the limit-order-book
DEX (contracts/dex.py, doc/24 §1): instead of waiting for a counterparty, the pool itself is always a
counterparty — anyone can swap against the reserves at a slippage-priced rate, and liquidity providers earn
a fee on every trade. No operator, no custodian: the contract escrows both reserves and settles atomically.

WHY AN AMM ("the market that's always open")
  An order book only trades when a maker and taker meet. An AMM holds a standing reserve of BOTH assets and
  quotes a price off the constant-product invariant R_bis * R_tok = k, so a swap ALWAYS executes (at a price
  that moves against size — slippage). The 0.3% fee on every swap stays in the pool, so the invariant k
  grows over time and each LP share is worth progressively more of the reserves: liquidity providers are
  paid by the flow they enable. That fee income — NOT a promise that prices only go up — is the real, honest
  reason an LP "wins": it is the on-chain version of the spread a Binance-style market maker earns. (Honest
  caveat, see doc/24 §3: an LP is exposed to impermanent loss when the price moves; the fee compensates for
  providing the liquidity, it is not free money. Nobody wins forever.)

WHAT IT DOES
  The contract issues ONE fixed-supply token (genesis-minted to the deployer, exactly like the DEX) and runs
  a single BIS<->token pool. Token balances are an internal ledger in contract storage; BIS lives in the
  contract's VM custody (R_bis == custody at all times). Anyone can:
    * ADD_LIQUIDITY    — deposit BIS (callvalue) + the proportional token, receive LP shares.
    * REMOVE_LIQUIDITY — burn LP shares, receive the proportional BIS (SYS_TRANSFER) + token back.
    * SWAP BIS->token  — attach BIS, receive token priced by x*y=k minus the 0.3% fee (slippage-aware).
    * SWAP token->BIS  — spend token, receive BIS (SYS_TRANSFER), same pricing the other way.
  Settlement is atomic: a failed require reverts — no storage commit, no transfer (and per the VM custody
  model, any BIS attached to a reverting call is refunded by the engine).

DEPLOY  (operation = "vm:deploy", openfield = build(admin_id, total_supply).hex())
  admin_id     = low 32 bits of the deployer's address int (party_id_of) — the only account that can mint.
  total_supply = the fixed token supply (<= 2^32-1 units), credited to the admin by FN_INIT (once). The
                 admin then seeds the pool with FN_ADD_LIQUIDITY and hands out token with FN_TRANSFER.

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>"); first 4 bytes = selector:
  FN_INIT       (0)  — admin-only, once: credit the whole token supply to the admin's balance.
  FN_TRANSFER   (1)  — sel | to(28 BE) | amt(4 BE). Move `amt` token from caller to `to`.
  FN_ADD_LIQ    (2)  — sel | tok_max(4 BE); ATTACH dBis BIS as value.
                         first add (pool empty): seeds the price — R_bis=dBis, R_tok=tok_max (debited from
                           the caller), mints dBis LP shares (less a locked MINIMUM_LIQUIDITY). Requires
                           dBis > MIN_LIQ.
                         later add: pulls dTok = ceil(dBis*R_tok/R_bis) token (<= tok_max) from the caller
                           and mints shares = floor(dBis*L/R_bis). Requires dBis <= R_bis (an add may at
                           most match the current reserve).
  FN_REMOVE_LIQ (3)  — sel | recipient(28 BE) | shares(4 BE). Burn `shares` LP from the caller; pay
                         floor(shares*R_bis/L) BIS to recipient(28) and credit floor(shares*R_tok/L) token
                         to the caller. recipient(28) must fold to the caller.
  FN_SWAP_B2T   (4)  — sel | min_tok_out(4 BE); ATTACH dBis BIS as value. Receive
                         out = floor(in_eff*R_tok/(R_bis+in_eff)) token, in_eff = floor(dBis*997/1000),
                         credited to the caller's token balance. Reverts if out < min_tok_out (slippage).
  FN_SWAP_T2B   (5)  — sel | recipient(28 BE) | tok_in(4 BE) | min_bis_out(4 BE). Spend tok_in token from
                         the caller; pay out = floor(in_eff*R_bis/(R_tok+in_eff)) BIS to recipient(28),
                         in_eff = floor(tok_in*997/1000). Reverts if out < min_bis_out. recipient folds to
                         the caller.
  A revert commits nothing; attach value ONLY to FN_ADD_LIQ / FN_SWAP_B2T. Each value-bearing op also caps
  the attached BIS at MAX_RESERVE (~10.7 BIS) BEFORE any arithmetic, so a too-large send simply reverts; and
  vm_engine refunds any deposit that does not fit a 32-bit word (> ~42.9 BIS) without executing, so the VM's
  32-bit callvalue can never diverge from the BIS the engine credited to custody (the custody == R_bis
  invariant holds for every reachable input — see doc/24 §2 and the security note in vm_engine._call).

STORAGE (32-bit word slots)
  1 = minted flag (0/1)     2 = R_bis (BIS reserve, units)   3 = R_tok (token reserve)   4 = L (total shares)
  0x10000000 | (party & 0x0FFFFFFF)  = token balance of a party (by CALLER fold)
  0x20000000 | (party & 0x0FFFFFFF)  = LP-share balance of a party

LIMITS (32-bit VM; mirrors the DEX's honest demo caps). Each reserve is capped at MAX_RESERVE = 2^30 units
(~10.7 BIS per side); every op that would grow a reserve past it reverts. That cap is exactly what makes the
constant-product math overflow-free on this 32-bit VM: the products in the swap/LP formulas are computed in a
full 64-bit (hi:lo) pair via asmtools' mulu64/divu64, and with both reserves <= 2^30 every intermediate fits
64 bits and every quotient fits the 32-bit result word. MINIMUM_LIQUIDITY (1000 units) is permanently locked
on the first add (Uniswap-style) so the pool can never be fully drained and re-seeded into a custody desync.
A 64-bit-aware store (a real C/Rust -> RV32I toolchain) would lift these caps; this is the clean, provably
in-range design for the base VM.
"""
import hashlib

from asmtools import (Asm, A0, A1, A2, A3, A4, A5, A6, S0, S1, S2, S3, T0, T1, T2, T3, T4, T5, T6, ZERO)

# selectors
FN_INIT = 0
FN_TRANSFER = 1
FN_ADD_LIQ = 2
FN_REMOVE_LIQ = 3
FN_SWAP_B2T = 4
FN_SWAP_T2B = 5

# fixed storage slots
S_MINTED = 1
S_RBIS = 2
S_RTOK = 3
S_LTOTAL = 4

# storage domains
TAG_BAL = 0x10000000
TAG_LP = 0x20000000
USER_MASK = 0x0FFFFFFF

# economics / safety constants
FEE_NUM = 997               # 0.3% fee: in_eff = in * 997 / 1000 stays in the pool, growing k for the LPs
FEE_DEN = 1000
MAX_RESERVE = 0x40000000    # 2^30 units (~10.7 BIS) per side — the cap that keeps all 64-bit math in range
MIN_LIQ = 1000              # MINIMUM_LIQUIDITY locked on the first add (never withdrawable)

# scratch memory (well past code+calldata; mem is 64 KiB)
SCRATCH_AMT = 8192          # 8-byte big-endian amount for SYS_TRANSFER


def party_id_of(address):
    """Low 32 bits of a Bismuth address int — how the VM (truncating CALLER to 32 bits) sees a party.
    Matches vm_engine._caller_int for 56-hex (RSA) addresses."""
    try:
        return int(str(address), 16) & 0xFFFFFFFF
    except Exception:
        return int.from_bytes(hashlib.blake2b(str(address).encode(), digest_size=32).digest(),
                              "big") & 0xFFFFFFFF


def _load_be32(a, rd, base_reg, off):
    """rd = 32-bit big-endian word at [base_reg+off]. Clobbers A2 (and rd)."""
    a.lbu(rd, base_reg, off)
    a.slli(rd, rd, 8)
    a.lbu(A2, base_reg, off + 1); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8)
    a.lbu(A2, base_reg, off + 2); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8)
    a.lbu(A2, base_reg, off + 3); a.or_(rd, rd, A2)


def _bal_key(a, dst, fold_reg):
    """dst = TAG_BAL | (fold_reg & USER_MASK). Clobbers dst, A5, A6; fold_reg preserved."""
    a.li(A5, USER_MASK)
    a.and_(dst, fold_reg, A5)
    a.li(A6, TAG_BAL)
    a.or_(dst, dst, A6)


def _lp_key(a, dst, fold_reg):
    """dst = TAG_LP | (fold_reg & USER_MASK). Clobbers dst, A5, A6; fold_reg preserved."""
    a.li(A5, USER_MASK)
    a.and_(dst, fold_reg, A5)
    a.li(A6, TAG_LP)
    a.or_(dst, dst, A6)


def _credit(a, key_reg, amt_reg):
    """storage[key] += amt. Uses A4."""
    a.sload_to(A4, key_reg)
    a.add(A4, A4, amt_reg)
    a.sstore(key_reg, A4)


def _debit_or_revert(a, key_reg, amt_reg):
    """storage[key] -= amt, reverting (illegal-instruction path) if balance < amt. Uses A4."""
    a.sload_to(A4, key_reg)
    a.bltu(A4, amt_reg, "revert")
    a.sub(A4, A4, amt_reg)
    a.sstore(key_reg, A4)


def _pay(a, amount_reg, addr_ptr_reg):
    """SYS_TRANSFER `amount_reg` units to the 28-byte address at addr_ptr_reg (e.g. calldata+4). Writes the
    amount as 8-byte BE at SCRATCH_AMT. amount_reg / addr_ptr_reg must not be A3 or A6."""
    a.li(A3, SCRATCH_AMT)
    a.store_u64_be(amount_reg, A3, 0)
    a.transfer(addr_ptr_reg, A3)


def _muldiv(a, dst, ra, rb, rc, ceil=False):
    """dst = (ra * rb) / rc, unsigned, computed in a full 64-bit intermediate (so ra*rb does not overflow
    32 bits) via mulu64 + divu64. `ceil` rounds up: dst = ceil(ra*rb/rc) = (ra*rb + rc - 1)/rc.

    Register discipline (verified against the macro internals in asmtools): mulu64/divu64 here use ONLY the
    scratch set {A2, A3, A4, A5, T0, T3, T4, T5} (+ A6 inner scratch). So `dst, ra, rb, rc` must each be one
    of the macro-SAFE registers {S0, S1, S2, S3, T1, T2, T6, A0, A1} — never a scratch register. ra/rb/rc
    are preserved (the macros copy them); dst receives the quotient. Callers must guarantee the true
    quotient fits 32 bits (it always does within MAX_RESERVE — see the module header)."""
    safe = {S0, S1, S2, S3, T1, T2, T6, A0, A1}
    assert ra in safe and rb in safe and rc in safe and dst in safe, "muldiv operands must be macro-safe regs"
    a.mv(A4, ra); a.mv(A5, rb)
    a.mulu64(A2, A3, A4, A5, m_hi=T0, m_lo=T3, bits=T4, carry=T5)     # (A2:A3) = ra * rb (64-bit)
    if ceil:
        a.addi(A4, rc, -1)                # A4 = rc - 1
        a.add(A3, A3, A4)                 # lo += (rc-1)
        a.sltu(A5, A3, A4)                # carry out of the low word
        a.add(A2, A2, A5)                 # hi += carry  (product < 2^60, +rc-1 cannot overflow 64 bits)
    a.mv(A5, rc)
    a.divu64(A4, A2, A3, A5, rem=T3, bit=T4, i=T5)                    # A4 = (A2:A3) / rc  (32-bit quotient)
    a.mv(dst, A4)


def _guard_max(a, value_reg):
    """revert if value_reg > MAX_RESERVE. Uses A2."""
    a.li(A2, MAX_RESERVE)
    a.bltu(A2, value_reg, "revert")       # MAX < value -> value exceeds the cap -> revert


def _require_no_value(a):
    """revert if BIS was attached to a call that does NOT consume callvalue, so the engine refunds it on the
    revert and custody == R_bis holds for every reachable call. Uses A2 (a0 is dead at handler entry)."""
    a.callvalue(A2); a.bne(A2, ZERO, "revert")


def build(admin_id, total_supply):
    """Assemble the AMM bytecode with `admin_id` (32-bit) as the genesis minter and `total_supply` baked in."""
    if not (0 <= int(total_supply) <= 0xFFFFFFFF):
        raise ValueError("total_supply must fit a 32-bit word (<= 2^32-1 units)")
    a = Asm()

    # dispatch: S0 = calldata ptr (stable), T0 = selector, T1 = caller fold, T2 = caller & USER_MASK.
    # S0, T1, T2 are preserved across the whole call (the 64-bit macros never touch them).
    a.mv(S0, A0)
    _load_be32(a, T0, S0, 0)
    a.caller(T1)
    a.li(A5, USER_MASK); a.and_(T2, T1, A5)

    a.li(A2, FN_INIT);        a.beq(T0, A2, "init")
    a.li(A2, FN_TRANSFER);    a.beq(T0, A2, "xfer")
    a.li(A2, FN_ADD_LIQ);     a.beq(T0, A2, "add")
    a.li(A2, FN_REMOVE_LIQ);  a.beq(T0, A2, "remove")
    a.li(A2, FN_SWAP_B2T);    a.beq(T0, A2, "swap_b2t")
    a.li(A2, FN_SWAP_T2B);    a.beq(T0, A2, "swap_t2b")
    a.j("revert")

    # --- FN_INIT: admin-only, once -> bal[admin] = total_supply --------------------------------------
    a.label("init")
    _require_no_value(a)
    a.li(A2, admin_id); a.bne(T1, A2, "revert")
    a.li(A3, S_MINTED); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")
    _bal_key(a, T6, T1)
    a.li(A4, int(total_supply)); a.sstore(T6, A4)
    a.li(A3, S_MINTED); a.li(A4, 1); a.sstore(A3, A4)
    a.halt()

    # --- FN_TRANSFER(to28, amt): move token caller -> to ---------------------------------------------
    a.label("xfer")
    _require_no_value(a)
    _load_be32(a, T3, S0, 32)                    # amt
    a.beq(T3, ZERO, "revert")
    _bal_key(a, T4, T1)
    _debit_or_revert(a, T4, T3)                  # debit caller
    _load_be32(a, T5, S0, 28)                    # recipient fold = low 32 of to(28)
    _bal_key(a, T6, T5)
    _credit(a, T6, T3)                           # credit recipient
    a.halt()

    # --- FN_ADD_LIQ(tok_max) + attach dBis: provide liquidity ----------------------------------------
    a.label("add")
    a.callvalue(T3)                              # T3 = dBis (attached value)
    a.beq(T3, ZERO, "revert")
    _load_be32(a, T4, S0, 4)                     # T4 = tok_max (token the caller will allow to be pulled)
    a.beq(T4, ZERO, "revert")
    a.li(A3, S_LTOTAL); a.sload_to(S1, A3)       # S1 = L (total LP shares)
    a.bne(S1, ZERO, "add_more")                  # pool already seeded -> proportional add

    # first add: seed the price. R_bis=dBis, R_tok=tok_max; mint dBis shares, lock MIN_LIQ of them.
    a.li(A2, MIN_LIQ); a.bgeu(A2, T3, "revert")  # require dBis > MIN_LIQ (caller must get positive shares)
    _guard_max(a, T3)                            # dBis <= MAX
    _guard_max(a, T4)                            # tok_max <= MAX
    _bal_key(a, T5, T1)
    _debit_or_revert(a, T5, T4)                  # pull the full tok_max from the caller as the token side
    a.li(A3, S_RBIS); a.sstore(A3, T3)           # R_bis = dBis
    a.li(A3, S_RTOK); a.sstore(A3, T4)           # R_tok = tok_max
    a.li(A3, S_LTOTAL); a.sstore(A3, T3)         # L = dBis (MIN_LIQ of it is locked / unowned)
    a.li(A2, MIN_LIQ); a.sub(T6, T3, A2)         # caller shares = dBis - MIN_LIQ
    _lp_key(a, T5, T1); _credit(a, T5, T6)
    a.halt()

    # later add: dTok = ceil(dBis*R_tok/R_bis) <= tok_max; shares = floor(dBis*L/R_bis). dBis must <= R_bis.
    # S1 = L (from the prelude). dBis rides A1 only ACROSS THE SYSCALL-FREE _muldiv calls (the 64-bit macros
    # never touch A1); it is reloaded via callvalue for the R_bis += dBis step, since the debit's sstore
    # below clobbers A1 (A0/A1 are the syscall argument registers). shares stay in T6, dTok in S1 (both
    # syscall-safe). tok_max is reloaded from calldata after the macros clobber the temporaries.
    a.label("add_more")
    a.callvalue(A1)                              # A1 = dBis (for the macros only)
    a.li(A3, S_RBIS); a.sload_to(S2, A3)         # S2 = R_bis
    a.li(A3, S_RTOK); a.sload_to(S3, A3)         # S3 = R_tok
    a.bltu(S2, A1, "revert")                     # require dBis(A1) <= R_bis(S2) (an add <= the reserve)
    _muldiv(a, T6, A1, S1, S2, ceil=False)       # T6 = shares = floor(dBis * L / R_bis)
    a.beq(T6, ZERO, "revert")                    # must mint > 0 shares
    _muldiv(a, S1, A1, S3, S2, ceil=True)        # S1 = dTok = ceil(dBis * R_tok / R_bis)  (reuse S1)
    _load_be32(a, T4, S0, 4)                     # reload tok_max (clobbered by the macros)
    a.bltu(T4, S1, "revert")                     # require dTok(S1) <= tok_max(T4)
    # debit caller token by dTok(S1)  (this sstore clobbers A1 -> dBis is reloaded next)
    _bal_key(a, T4, T1); _debit_or_revert(a, T4, S1)
    # R_bis += dBis : reload dBis via callvalue (A1), guard <= MAX, store
    a.callvalue(A1)                              # A1 = dBis again
    a.li(A3, S_RBIS); a.sload_to(A4, A3); a.add(T4, A4, A1); _guard_max(a, T4)
    a.li(A3, S_RBIS); a.sstore(A3, T4)
    # R_tok += dTok(S1) : guard <= MAX, store
    a.li(A3, S_RTOK); a.sload_to(A4, A3); a.add(T4, A4, S1); _guard_max(a, T4)
    a.li(A3, S_RTOK); a.sstore(A3, T4)
    # L += shares(T6) : GUARD <= MAX. Without this a pool seeded with tiny R_tok and huge L lets L double on
    # each proportional add (dTok stays tiny, so its guard never fires) until L wraps past 2^32, minting
    # phantom shares and corrupting reserves on a later remove. (Audit finding — same class as the DEX caps.)
    a.li(A3, S_LTOTAL); a.sload_to(A4, A3); a.add(T4, A4, T6); _guard_max(a, T4)
    a.li(A3, S_LTOTAL); a.sstore(A3, T4)
    # credit caller LP shares(T6)
    _lp_key(a, T4, T1); _credit(a, T4, T6)
    a.halt()

    # --- FN_REMOVE_LIQ(recipient28, shares): burn shares, pay BIS + token back ------------------------
    a.label("remove")
    _require_no_value(a)
    _load_be32(a, T3, S0, 32)                    # T3 = shares to burn (s)
    a.beq(T3, ZERO, "revert")
    _load_be32(a, T4, S0, 28); a.bne(T4, T1, "revert")   # recipient(28) low word must equal the full caller
    _lp_key(a, T5, T1); _debit_or_revert(a, T5, T3)   # burn s from the caller's LP balance
    a.li(A3, S_LTOTAL); a.sload_to(S1, A3)       # S1 = L
    a.li(A3, S_RBIS); a.sload_to(S2, A3)         # S2 = R_bis
    a.li(A3, S_RTOK); a.sload_to(S3, A3)         # S3 = R_tok
    a.mv(T6, T3)                                 # T6 = s (macro-safe; T3 will be clobbered)
    _muldiv(a, S2, T6, S2, S1, ceil=False)       # S2 = outBis = floor(s * R_bis / L)   (reuse S2)
    _muldiv(a, S3, T6, S3, S1, ceil=False)       # S3 = outTok = floor(s * R_tok / L)   (reuse S3)
    # R_bis -= outBis ; R_tok -= outTok ; L -= s
    a.li(A3, S_RBIS); a.sload_to(A4, A3); a.sub(A4, A4, S2); a.sstore(A3, A4)
    a.li(A3, S_RTOK); a.sload_to(A4, A3); a.sub(A4, A4, S3); a.sstore(A3, A4)
    a.li(A3, S_LTOTAL); a.sload_to(A4, A3); a.sub(A4, A4, T6); a.sstore(A3, A4)
    # credit the caller's token balance with outTok(S3)
    _bal_key(a, T4, T1); _credit(a, T4, S3)
    # pay outBis(S2) BIS to recipient(28) at calldata+4
    a.addi(A4, S0, 4)
    _pay(a, S2, A4)
    a.halt()

    # --- FN_SWAP_B2T(min_tok_out) + attach dBis: BIS -> token ----------------------------------------
    # Same A0/A1 discipline as swap_t2b: `in` lives in S1 (syscall-safe) and is reloaded via callvalue for
    # the final R_bis += in; the fee constants ride A0/A1 only inside the syscall-free _muldiv.
    a.label("swap_b2t")
    a.callvalue(S1)                              # S1 = in (BIS), syscall-safe
    a.beq(S1, ZERO, "revert")
    _guard_max(a, S1)                            # CRITICAL: bound the RAW in <= MAX before any add, so the
                                                 # R_bis+in below cannot wrap mod 2^32 (a wrapped sum would
                                                 # pass the cap guard yet desync custody and drain the pool).
    a.li(A3, S_RBIS); a.sload_to(S2, A3)         # S2 = R_bis
    a.li(A3, S_RTOK); a.sload_to(S3, A3)         # S3 = R_tok
    a.add(T6, S2, S1)                            # new R_bis = R_bis + in (<= 2*MAX < 2^32: cannot wrap)
    _guard_max(a, T6)                            # and the post-swap reserve must still be <= MAX
    # in_eff = floor(in * 997 / 1000)  — the 0.3% fee stays in the pool (grows k for the LPs)
    a.li(A0, FEE_NUM); a.li(A1, FEE_DEN)         # fee constants in A0/A1, used only inside the macro
    _muldiv(a, S1, S1, A0, A1, ceil=False)       # S1 = in_eff (overwrites in; reloaded at commit)
    a.beq(S1, ZERO, "revert")
    # out = floor(in_eff * R_tok / (R_bis + in_eff))
    a.add(T6, S2, S1)                            # T6 = denom = R_bis + in_eff (<= 2*MAX, fits 32 bits)
    _muldiv(a, S1, S1, S3, T6, ceil=False)       # S1 = out (reuse S1: in_eff -> out)
    a.beq(S1, ZERO, "revert")
    a.bltu(S3, S1, "revert")                     # defensive: out <= R_tok (can never drain the token side)
    _load_be32(a, T4, S0, 4)                     # min_tok_out
    a.bltu(S1, T4, "revert")                     # slippage guard: out >= min_tok_out
    # commit: R_bis += in ; R_tok -= out(S1)
    a.callvalue(T6)                              # reload in (S1 now holds out)
    a.li(A3, S_RBIS); a.sload_to(A4, A3); a.add(A4, A4, T6); a.sstore(A3, A4)
    a.li(A3, S_RTOK); a.sload_to(A4, A3); a.sub(A4, A4, S1); a.sstore(A3, A4)
    # credit caller token by out(S1)
    _bal_key(a, T4, T1); _credit(a, T4, S1)
    a.halt()

    # --- FN_SWAP_T2B(recipient28, tok_in, min_bis_out): token -> BIS ---------------------------------
    # NOTE: A0/A1 are the syscall argument registers (sstore/transfer clobber A1, sload/caller/callvalue
    # clobber A0), so a value must NEVER be held in A0/A1 across a syscall. `in` lives in S1 (syscall-safe)
    # and is reloaded from calldata for the final R_tok += in; the fee constants ride A0/A1 only inside the
    # syscall-free _muldiv. S2=R_bis, S3=R_tok (only read), S1 carries in -> in_eff -> out.
    a.label("swap_t2b")
    _require_no_value(a)
    _load_be32(a, T4, S0, 28); a.bne(T4, T1, "revert")   # recipient(28) low word must equal the full caller
    _load_be32(a, S1, S0, 32)                    # S1 = tok_in (in), syscall-safe
    a.beq(S1, ZERO, "revert")
    _guard_max(a, S1)                            # bound the RAW in <= MAX before any add (so R_tok+in below
                                                 # cannot wrap; also makes safety independent of total_supply)
    a.li(A3, S_RBIS); a.sload_to(S2, A3)         # S2 = R_bis
    a.li(A3, S_RTOK); a.sload_to(S3, A3)         # S3 = R_tok
    a.add(T6, S3, S1)                            # new R_tok = R_tok + in (<= 2*MAX < 2^32: cannot wrap)
    _guard_max(a, T6)                            # and the post-swap reserve must still be <= MAX
    # debit caller token by in(S1)  (the sstore clobbers A0/A1; S1 survives)
    _bal_key(a, T5, T1); _debit_or_revert(a, T5, S1)
    # in_eff = floor(in * 997 / 1000)
    a.li(A0, FEE_NUM); a.li(A1, FEE_DEN)         # fee constants in A0/A1, used only inside the macro
    _muldiv(a, S1, S1, A0, A1, ceil=False)       # S1 = in_eff (overwrites in; reloaded at commit)
    a.beq(S1, ZERO, "revert")
    # out = floor(in_eff * R_bis / (R_tok + in_eff))   [reserves swapped: token in, BIS out]
    a.add(T6, S3, S1)                            # T6 = denom = R_tok + in_eff
    _muldiv(a, S1, S1, S2, T6, ceil=False)       # S1 = out (BIS)
    a.beq(S1, ZERO, "revert")
    a.bltu(S2, S1, "revert")                     # defensive: out <= R_bis (can never drain the BIS side)
    _load_be32(a, T4, S0, 36)                    # min_bis_out
    a.bltu(S1, T4, "revert")                     # slippage guard
    # commit: R_tok += in ; R_bis -= out(S1)
    _load_be32(a, T6, S0, 32)                    # reload in (S1 now holds out)
    a.li(A3, S_RTOK); a.sload_to(A4, A3); a.add(A4, A4, T6); a.sstore(A3, A4)
    a.li(A3, S_RBIS); a.sload_to(A4, A3); a.sub(A4, A4, S1); a.sstore(A3, A4)
    # pay out(S1) BIS to recipient(28) at calldata+4
    a.addi(A4, S0, 4)
    _pay(a, S1, A4)
    a.halt()

    # --- revert: illegal instruction -> deterministic revert (no commit, no transfer) ----------------
    a.label("revert")
    a.raw(0)

    return a.assemble()


# --- read helpers (for tests / a relay reading /api/vm/contract storage) --------------------------
def balance_key(party_id):
    return TAG_BAL | (party_id & USER_MASK)


def lp_key(party_id):
    return TAG_LP | (party_id & USER_MASK)
