"""
router.py — a MULTI-POOL automated market maker / router: one contract that hosts many BIS-paired
constant-product pools and swaps ANY token to ANY other token atomically by routing through BIS. The
multi-pool generalization of the single-pool AMM (contracts/amm.py, doc/24 §2), and the answer to
"any token can swap to any other."

WHY ONE CONTRACT (not a router over separate pools)
  The Bismuth in-core VM has NO cross-contract calls (vm_engine executes exactly one contract per vm:call),
  so a router cannot atomically call pool-A then pool-B if they are separate deployments. The fix is the
  Balancer-V2 "Vault" model: ONE contract holds every pool's reserves, so a token->token route is just two
  hops in a single execution against this contract's own storage — fully atomic, no cross-contract calls.

WHAT IT DOES
  The admin CREATEs up to MAX_TOKENS tokens (each a fixed supply minted to the admin). Each token tid gets
  its own constant-product pool against native BIS: reserves R_bis[tid], R_tok[tid], LP shares L[tid]. BIS
  lives in the contract's VM custody; the total custody equals the SUM of every pool's R_bis. Token balances
  are an internal per-(tid,party) ledger. Anyone can:
    * ADD / REMOVE liquidity to a specific pool tid (same math as the single-pool AMM).
    * SWAP BIS<->token tid (single hop, slippage-priced off R_bis[tid]*R_tok[tid]=k, 0.3% fee).
    * SWAP token A -> token B  — the router hop: A->BIS through pool A, then BIS->B through pool B, ATOMIC in
      one call. The intermediate BIS never leaves custody (it moves from R_bis[A] to R_bis[B]); the 0.3% fee
      is taken on BOTH hops, so both pools' LPs earn. Either both legs settle or the whole call reverts.

DEPLOY  (operation = "vm:deploy", openfield = build(admin_id).hex())
  admin_id = low 32 bits of the deployer's address int (party_id_of) — the only account that can CREATE
  tokens. (Token creation is admin-gated like the AMM's mint; everything else is permissionless.)

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>"); first 4 bytes = selector:
  FN_CREATE     (0)  — sel | supply(4). admin-only. Mints a new token (tid = next counter) with `supply`
                       units to the admin and returns the new tid. Reverts past MAX_TOKENS.
  FN_TRANSFER   (1)  — sel | tid(4) | to(28) | amt(4). Move `amt` of token tid from caller to `to`.
  FN_ADD_LIQ    (2)  — sel | tid(4) | tok_max(4); ATTACH dBis. Add liquidity to pool tid (first add seeds
                       the price + locks MIN_LIQ; later adds are proportional, dBis <= R_bis[tid]).
  FN_REMOVE_LIQ (3)  — sel | tid(4) | recipient(28) | shares(4). Burn shares of pool tid -> proportional BIS
                       (to recipient) + token tid (to caller). recipient(28) low word must equal the caller.
  FN_SWAP_B2T   (4)  — sel | tid(4) | min_tok_out(4); ATTACH dBis. BIS -> token tid (credited to caller).
  FN_SWAP_T2B   (5)  — sel | tid(4) | recipient(28) | tok_in(4) | min_bis_out(4). token tid -> BIS (to recip).
  FN_SWAP_T2T   (6)  — sel | tid_in(4) | tid_out(4) | amt_in(4) | min_out(4). token tid_in -> token tid_out,
                       routed A->BIS->B atomically. Reverts if out < min_out, if tid_in==tid_out, or if
                       either tid is not created.
  A revert commits nothing; attach value ONLY to FN_ADD_LIQ / FN_SWAP_B2T. Each value-bearing op caps the
  attached BIS at MAX_RESERVE before any arithmetic (and vm_engine refunds any deposit that exceeds a 32-bit
  word), so the VM's 32-bit callvalue never diverges from the BIS credited to custody.

STORAGE (32-bit word slots)
  0 = next_tid (number of tokens created; tid in 0..MAX_TOKENS-1)
  0x10000000 | (tid<<4) | field   = pool[tid]: field 0 R_bis, 1 R_tok, 2 L (total shares)
  0x20000000 | (tid<<24) | (party & 0x00FFFFFF)  = token balance of a party in token tid
  0x30000000 | (tid<<24) | (party & 0x00FFFFFF)  = LP-share balance of a party in pool tid

LIMITS (32-bit VM; mirrors amm.py's honest caps). MAX_TOKENS = 16 (the tid nibble), each reserve capped at
MAX_RESERVE = 2^30 units (~10.7 BIS) per side so all the 64-bit mulu64/divu64 math stays in range — including
the router hop, whose every intermediate is bounded by those same per-pool caps (the mid BIS amount <=
R_bis[A] <= MAX). Party fold is the low 24 bits of the address here (vs 28 in the single-pool AMM) to make
room for the tid in the 32-bit key — so distinct addresses sharing their low 24 bits would share a balance
(a documented demo cap; a 64-bit-key store lifts both this and the reserve cap). MIN_LIQ (1000 units) is
locked on each pool's first add. Register discipline matches amm.py: A0/A1 are syscall argument registers
(sstore/transfer clobber them), so live values ride only {T1,T2,T6,S0,S1,S2,S3} or are reloaded.
"""
import hashlib

from asmtools import (Asm, A0, A1, A2, A3, A4, A5, A6, S0, S1, S2, S3, T0, T1, T2, T3, T4, T5, T6, ZERO)

# selectors
FN_CREATE = 0
FN_TRANSFER = 1
FN_ADD_LIQ = 2
FN_REMOVE_LIQ = 3
FN_SWAP_B2T = 4
FN_SWAP_T2B = 5
FN_SWAP_T2T = 6

# fixed storage
S_NEXT_TID = 0

# pool fields (within TAG_POOL | (tid<<4) | field)
POOL_RBIS = 0
POOL_RTOK = 1
POOL_L = 2

# storage domains
TAG_POOL = 0x10000000
TAG_BAL = 0x20000000
TAG_LP = 0x30000000
USER_MASK = 0x00FFFFFF       # 24-bit party fold; tid occupies bits 24..27 of a balance/LP key

# economics / safety
FEE_NUM = 997
FEE_DEN = 1000
MAX_RESERVE = 0x40000000     # 2^30 units (~10.7 BIS) per pool side
MIN_LIQ = 1000
MAX_TOKENS = 16              # tid in 0..15

SCRATCH_AMT = 16384          # 8-byte BE transfer amount, well past code + calldata


def party_id_of(address):
    """Low 32 bits of a Bismuth address int — how the VM (truncating CALLER to 32 bits) sees a party."""
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


def _pool_key(a, dst, tid_reg, field):
    """dst = TAG_POOL | (tid_reg<<4) | field. Clobbers dst, A6; tid_reg preserved."""
    a.slli(dst, tid_reg, 4)
    if field:
        a.ori(dst, dst, field)
    a.li(A6, TAG_POOL); a.or_(dst, dst, A6)


def _bal_key(a, dst, tid_reg, party_reg):
    """dst = TAG_BAL | (tid_reg<<24) | (party_reg & USER_MASK). Clobbers dst, A5, A6; inputs preserved."""
    a.slli(dst, tid_reg, 24)
    a.li(A6, USER_MASK); a.and_(A5, party_reg, A6); a.or_(dst, dst, A5)
    a.li(A6, TAG_BAL); a.or_(dst, dst, A6)


def _lp_key(a, dst, tid_reg, party_reg):
    """dst = TAG_LP | (tid_reg<<24) | (party_reg & USER_MASK). Clobbers dst, A5, A6; inputs preserved."""
    a.slli(dst, tid_reg, 24)
    a.li(A6, USER_MASK); a.and_(A5, party_reg, A6); a.or_(dst, dst, A5)
    a.li(A6, TAG_LP); a.or_(dst, dst, A6)


def _credit(a, key_reg, amt_reg):
    """storage[key] += amt. Uses A4."""
    a.sload_to(A4, key_reg)
    a.add(A4, A4, amt_reg)
    a.sstore(key_reg, A4)


def _debit_or_revert(a, key_reg, amt_reg):
    """storage[key] -= amt, reverting if balance < amt. Uses A4. (sstore clobbers A0/A1.)"""
    a.sload_to(A4, key_reg)
    a.bltu(A4, amt_reg, "revert")
    a.sub(A4, A4, amt_reg)
    a.sstore(key_reg, A4)


def _pay(a, amount_reg, addr_ptr_reg):
    """SYS_TRANSFER `amount_reg` units to the 28-byte address at addr_ptr_reg. amount_reg/addr_ptr_reg must
    not be A3 or A6 (transfer also clobbers A0/A1)."""
    a.li(A3, SCRATCH_AMT)
    a.store_u64_be(amount_reg, A3, 0)
    a.transfer(addr_ptr_reg, A3)


def _guard_max(a, value_reg):
    """revert if value_reg > MAX_RESERVE. Uses A2."""
    a.li(A2, MAX_RESERVE)
    a.bltu(A2, value_reg, "revert")


def _require_tid(a, tid_reg):
    """revert unless tid_reg < next_tid (the token/pool exists). Uses A2, A3; tid_reg preserved. Since CREATE
    keeps next_tid <= MAX_TOKENS, this also bounds tid < MAX_TOKENS so tid<<24 / tid<<4 cannot spill into a
    neighbouring key domain."""
    a.li(A3, S_NEXT_TID); a.sload_to(A2, A3)
    a.bgeu(tid_reg, A2, "revert")


def _require_no_value(a):
    """revert if any BIS was attached to a call that does NOT consume callvalue, so the engine refunds it on
    the revert and custody == sum(R_bis) holds for every reachable call. Uses A2 (a0 is dead at handler
    entry); place as the FIRST instruction of a non-value handler."""
    a.callvalue(A2); a.bne(A2, ZERO, "revert")


def _muldiv(a, dst, ra, rb, rc, ceil=False):
    """dst = (ra*rb)/rc, unsigned, via a full 64-bit intermediate (mulu64 + divu64); `ceil` rounds up.
    The macros use ONLY the scratch set {A2,A3,A4,A5,T0,T3,T4,T5}+A6, so dst/ra/rb/rc must each be a
    macro-safe register {S0,S1,S2,S3,T1,T2,T6,A0,A1}; ra/rb/rc are preserved, dst gets the quotient. Callers
    must keep the true quotient < 2^32 (always true within MAX_RESERVE)."""
    safe = {S0, S1, S2, S3, T1, T2, T6, A0, A1}
    assert ra in safe and rb in safe and rc in safe and dst in safe, "muldiv operands must be macro-safe regs"
    a.mv(A4, ra); a.mv(A5, rb)
    a.mulu64(A2, A3, A4, A5, m_hi=T0, m_lo=T3, bits=T4, carry=T5)     # (A2:A3) = ra*rb
    if ceil:
        a.addi(A4, rc, -1)
        a.add(A3, A3, A4); a.sltu(A5, A3, A4); a.add(A2, A2, A5)      # +(rc-1) for ceil (cannot overflow 64b)
    a.mv(A5, rc)
    a.divu64(A4, A2, A3, A5, rem=T3, bit=T4, i=T5)                    # A4 = (A2:A3)/rc
    a.mv(dst, A4)


def _swap_math(a, in_reg, r_in_reg, r_out_reg, out_dst):
    """out_dst = floor( floor(in*997/1000) * r_out / (r_in + floor(in*997/1000)) ), the constant-product
    swap with the 0.3% fee. in_reg/r_in_reg/r_out_reg are macro-safe inputs (preserved); out_dst receives
    the result. out_dst MAY alias in_reg but MUST differ from r_in_reg and r_out_reg (the denom add reads
    r_in after out_dst has been written). Uses only the supplied regs plus A0/A1 (fee constants / denom),
    both macro-safe transients with no syscall here. Caller guarantees in, r_in, r_out <= MAX so every
    intermediate fits (product <= 2^60, divisor < 2^32)."""
    assert out_dst != r_in_reg and out_dst != r_out_reg, "_swap_math out_dst must differ from r_in/r_out"
    # in_eff = floor(in*997/1000) -> out_dst (temporary), then out = in_eff*r_out/(r_in+in_eff)
    a.li(A0, FEE_NUM); a.li(A1, FEE_DEN)
    _muldiv(a, out_dst, in_reg, A0, A1, ceil=False)      # out_dst = in_eff
    a.beq(out_dst, ZERO, "revert")                       # zero effective input -> revert
    a.add(A1, r_in_reg, out_dst)                         # A1 = denom = r_in + in_eff (macro-safe transient)
    _muldiv(a, out_dst, out_dst, r_out_reg, A1, ceil=False)   # out_dst = in_eff*r_out/denom


def build(admin_id):
    """Assemble the multi-pool router with `admin_id` (32-bit) as the only account that can CREATE tokens."""
    a = Asm()

    # dispatch: S0 = calldata ptr (stable), T0 = selector, T1 = caller (full low 32), T2 = caller fold (24)
    a.mv(S0, A0)
    _load_be32(a, T0, S0, 0)
    a.caller(T1)
    a.li(A5, USER_MASK); a.and_(T2, T1, A5)

    a.li(A2, FN_CREATE);     a.beq(T0, A2, "create")
    a.li(A2, FN_TRANSFER);   a.beq(T0, A2, "xfer")
    a.li(A2, FN_ADD_LIQ);    a.beq(T0, A2, "add")
    a.li(A2, FN_REMOVE_LIQ); a.beq(T0, A2, "remove")
    a.li(A2, FN_SWAP_B2T);   a.beq(T0, A2, "swap_b2t")
    a.li(A2, FN_SWAP_T2B);   a.beq(T0, A2, "swap_t2b")
    a.li(A2, FN_SWAP_T2T);   a.beq(T0, A2, "swap_t2t")
    a.j("revert")

    # --- FN_CREATE(supply): admin-only -> mint new token tid=next_tid to admin, return tid --------------
    a.label("create")
    _require_no_value(a)
    a.li(A2, admin_id); a.bne(T1, A2, "revert")
    a.li(A3, S_NEXT_TID); a.sload_to(S1, A3)     # S1 = next_tid (the new token's id)
    a.li(A2, MAX_TOKENS); a.bgeu(S1, A2, "revert")
    _load_be32(a, T3, S0, 4)                     # supply
    a.beq(T3, ZERO, "revert")
    _bal_key(a, T4, S1, T2); a.sstore(T4, T3)    # bal[new tid][admin] = supply (was 0)
    a.li(A3, S_NEXT_TID); a.addi(A4, S1, 1); a.sstore(A3, A4)
    a.mv(A0, S1); a.ret()                         # return the new tid

    # --- FN_TRANSFER(tid, to28, amt) -----------------------------------------------------------------
    a.label("xfer")
    _require_no_value(a)
    _load_be32(a, T6, S0, 4); _require_tid(a, T6)        # T6 = tid
    _load_be32(a, T3, S0, 36)                    # amt (to(28) at 8..36)
    a.beq(T3, ZERO, "revert")
    _bal_key(a, T4, T6, T2); _debit_or_revert(a, T4, T3)
    _load_be32(a, T5, S0, 32)                    # recipient low word
    _bal_key(a, T4, T6, T5); _credit(a, T4, T3)
    a.halt()

    # --- FN_ADD_LIQ(tid, tok_max) + attach dBis ------------------------------------------------------
    a.label("add")
    _load_be32(a, T6, S0, 4); _require_tid(a, T6)        # T6 = tid (held; syscall+macro safe)
    a.callvalue(T3)                              # dBis
    a.beq(T3, ZERO, "revert")
    _load_be32(a, T4, S0, 8)                     # tok_max
    a.beq(T4, ZERO, "revert")
    _pool_key(a, A3, T6, POOL_L); a.sload_to(S1, A3)     # S1 = L[tid]
    a.bne(S1, ZERO, "add_more")

    # first add: seed pool tid (R_bis=dBis, R_tok=tok_max), mint dBis shares, lock MIN_LIQ.
    a.li(A2, MIN_LIQ); a.bgeu(A2, T3, "revert")  # dBis > MIN_LIQ
    _guard_max(a, T3); _guard_max(a, T4)
    _bal_key(a, T5, T6, T2); _debit_or_revert(a, T5, T4)         # pull tok_max from caller
    _pool_key(a, A3, T6, POOL_RBIS); a.sstore(A3, T3)
    _pool_key(a, A3, T6, POOL_RTOK); a.sstore(A3, T4)
    _pool_key(a, A3, T6, POOL_L);    a.sstore(A3, T3)            # L = dBis
    a.li(A2, MIN_LIQ); a.sub(T5, T3, A2)                         # caller shares = dBis - MIN_LIQ
    _lp_key(a, T4, T6, T2); _credit(a, T4, T5)
    a.halt()

    # later add: shares=floor(dBis*L/R_bis) (S3), dTok=ceil(dBis*R_tok/R_bis) (S1); dBis<=R_bis. dBis rides
    # A1 across the syscall-free macros, reloaded via callvalue for the R_bis += dBis after the debit sstore.
    a.label("add_more")
    a.callvalue(A1)                              # A1 = dBis (for the macros)
    _pool_key(a, A3, T6, POOL_RBIS); a.sload_to(S2, A3)  # S2 = R_bis
    a.bltu(S2, A1, "revert")                     # dBis <= R_bis
    _muldiv(a, S3, A1, S1, S2, ceil=False)       # S3 = shares = floor(dBis*L/R_bis)
    a.beq(S3, ZERO, "revert")
    _pool_key(a, A3, T6, POOL_RTOK); a.sload_to(S1, A3)  # S1 = R_tok (L no longer needed in-reg)
    _muldiv(a, S1, A1, S1, S2, ceil=True)        # S1 = dTok = ceil(dBis*R_tok/R_bis)
    _load_be32(a, T3, S0, 8)                     # reload tok_max
    a.bltu(T3, S1, "revert")                     # dTok <= tok_max
    _bal_key(a, T4, T6, T2); _debit_or_revert(a, T4, S1)        # debit caller token by dTok (clobbers A1)
    # R_bis += dBis
    a.callvalue(A1)                              # reload dBis
    _pool_key(a, T4, T6, POOL_RBIS); a.sload_to(A4, T4); a.add(T5, A4, A1); _guard_max(a, T5); a.sstore(T4, T5)
    # R_tok += dTok(S1)
    _pool_key(a, T4, T6, POOL_RTOK); a.sload_to(A4, T4); a.add(T5, A4, S1); _guard_max(a, T5); a.sstore(T4, T5)
    # L += shares(S3) : GUARD <= MAX. Without this a pool seeded with tiny R_tok and huge L (the seed path
    # ties L to dBis, not to R_tok) lets L double on each proportional add while dTok stays tiny — L would
    # wrap past 2^32, minting phantom shares and corrupting the reserves on a later remove. (Audit, doc/24 §3.)
    _pool_key(a, T4, T6, POOL_L); a.sload_to(A4, T4); a.add(T5, A4, S3); _guard_max(a, T5); a.sstore(T4, T5)
    # credit caller LP shares(S3)
    _lp_key(a, T4, T6, T2); _credit(a, T4, S3)
    a.halt()

    # --- FN_REMOVE_LIQ(tid, recipient28, shares) -----------------------------------------------------
    # s rides S1 (loaded from calldata, never modified), L rides S3; outBis/outTok cycle through S2.
    a.label("remove")
    _require_no_value(a)
    _load_be32(a, T6, S0, 4); _require_tid(a, T6)        # T6 = tid
    _load_be32(a, T4, S0, 32); a.bne(T4, T1, "revert")   # recipient(28) low word == full caller
    _load_be32(a, S1, S0, 36)                    # S1 = shares (s)
    a.beq(S1, ZERO, "revert")
    _lp_key(a, T5, T6, T2); _debit_or_revert(a, T5, S1)  # burn s from caller LP
    _pool_key(a, A3, T6, POOL_L); a.sload_to(S3, A3)     # S3 = L
    # outBis = floor(s*R_bis/L) -> S2 ; then R_bis -= outBis ; then pay outBis to recipient
    _pool_key(a, A3, T6, POOL_RBIS); a.sload_to(S2, A3)  # S2 = R_bis
    _muldiv(a, S2, S1, S2, S3, ceil=False)       # S2 = outBis
    _pool_key(a, T4, T6, POOL_RBIS); a.sload_to(A4, T4); a.sub(A4, A4, S2); a.sstore(T4, A4)   # R_bis -= outBis
    a.addi(A4, S0, 8); _pay(a, S2, A4)           # pay outBis BIS to recipient(28) at calldata+8
    # outTok = floor(s*R_tok/L) -> S2 ; then R_tok -= outTok ; credit caller token
    _pool_key(a, A3, T6, POOL_RTOK); a.sload_to(S2, A3)  # S2 = R_tok
    _muldiv(a, S2, S1, S2, S3, ceil=False)       # S2 = outTok
    _pool_key(a, T4, T6, POOL_RTOK); a.sload_to(A4, T4); a.sub(A4, A4, S2); a.sstore(T4, A4)   # R_tok -= outTok
    # L -= s(S1)
    _pool_key(a, T4, T6, POOL_L); a.sload_to(A4, T4); a.sub(A4, A4, S1); a.sstore(T4, A4)
    # credit caller token tid += outTok(S2)
    _bal_key(a, T4, T6, T2); _credit(a, T4, S2)
    a.halt()

    # --- FN_SWAP_B2T(tid, min_tok_out) + attach dBis: BIS -> token tid -------------------------------
    a.label("swap_b2t")
    _load_be32(a, T6, S0, 4); _require_tid(a, T6)        # T6 = tid
    a.callvalue(S1)                              # S1 = in (BIS)
    a.beq(S1, ZERO, "revert")
    _guard_max(a, S1)                            # raw in <= MAX before any add (no wrap)
    _pool_key(a, A3, T6, POOL_RBIS); a.sload_to(S2, A3)  # S2 = R_bis
    _pool_key(a, A3, T6, POOL_RTOK); a.sload_to(S3, A3)  # S3 = R_tok
    a.add(T4, S2, S1); _guard_max(a, T4)         # new R_bis <= MAX
    _swap_math(a, S1, S2, S3, S1)                # S1 = out (token); in/R_bis/R_tok = S1/S2/S3
    a.beq(S1, ZERO, "revert")
    a.bltu(S3, S1, "revert")                     # out <= R_tok
    _load_be32(a, T4, S0, 8); a.bltu(S1, T4, "revert")   # slippage: out >= min_tok_out
    # commit: R_bis += in ; R_tok -= out
    a.callvalue(T4)                              # reload in
    _pool_key(a, T5, T6, POOL_RBIS); a.sload_to(A4, T5); a.add(A4, A4, T4); a.sstore(T5, A4)
    _pool_key(a, T5, T6, POOL_RTOK); a.sload_to(A4, T5); a.sub(A4, A4, S1); a.sstore(T5, A4)
    _bal_key(a, T4, T6, T2); _credit(a, T4, S1)  # credit caller token tid += out
    a.halt()

    # --- FN_SWAP_T2B(tid, recipient28, tok_in, min_bis_out): token tid -> BIS ------------------------
    a.label("swap_t2b")
    _require_no_value(a)
    _load_be32(a, T6, S0, 4); _require_tid(a, T6)        # T6 = tid
    _load_be32(a, T4, S0, 32); a.bne(T4, T1, "revert")   # recipient(28) low word == full caller
    _load_be32(a, S1, S0, 36)                    # S1 = tok_in (in)
    a.beq(S1, ZERO, "revert")
    _guard_max(a, S1)                            # raw in <= MAX
    _pool_key(a, A3, T6, POOL_RBIS); a.sload_to(S2, A3)  # S2 = R_bis
    _pool_key(a, A3, T6, POOL_RTOK); a.sload_to(S3, A3)  # S3 = R_tok
    a.add(T4, S3, S1); _guard_max(a, T4)         # new R_tok <= MAX
    _bal_key(a, T5, T6, T2); _debit_or_revert(a, T5, S1) # debit caller token by in
    _swap_math(a, S1, S3, S2, S1)                # S1 = out (BIS); in/R_tok/R_bis = S1/S3/S2 (reserves swapped)
    a.beq(S1, ZERO, "revert")
    a.bltu(S2, S1, "revert")                     # out <= R_bis
    _load_be32(a, T4, S0, 40); a.bltu(S1, T4, "revert")  # slippage
    # commit: R_tok += in ; R_bis -= out
    _load_be32(a, T4, S0, 36)                    # reload in
    _pool_key(a, T5, T6, POOL_RTOK); a.sload_to(A4, T5); a.add(A4, A4, T4); a.sstore(T5, A4)
    _pool_key(a, T5, T6, POOL_RBIS); a.sload_to(A4, T5); a.sub(A4, A4, S1); a.sstore(T5, A4)
    a.addi(A4, S0, 8); _pay(a, S1, A4)           # pay out BIS to recipient(28) at calldata+8
    a.halt()

    # --- FN_SWAP_T2T(tid_in, tid_out, amt_in, min_out): token A -> BIS -> token B (atomic route) ------
    # bis_mid (S2) carries the intermediate BIS from pool A to pool B — it stays in custody (R_bis[A] down,
    # R_bis[B] up), so no SYS_TRANSFER and custody is unchanged. amt_in rides S1; tids reloaded from calldata.
    a.label("swap_t2t")
    _require_no_value(a)
    _load_be32(a, T3, S0, 4)                     # tid_in (T3)
    _load_be32(a, T4, S0, 8)                     # tid_out (T4)
    a.beq(T3, T4, "revert")                      # in != out
    a.li(A3, S_NEXT_TID); a.sload_to(A2, A3)     # next_tid
    a.bgeu(T3, A2, "revert"); a.bgeu(T4, A2, "revert")   # both tids created
    _load_be32(a, S1, S0, 12)                    # S1 = amt_in
    a.beq(S1, ZERO, "revert")
    _guard_max(a, S1)                            # raw amt_in <= MAX
    _bal_key(a, T5, T3, T2); _debit_or_revert(a, T5, S1) # debit caller token tid_in by amt_in

    # HOP 1: token tid_in -> BIS.  bis_mid(S2) = swap(amt_in; R_tok[A], R_bis[A]). amt_in stays in S1
    # (in_reg is preserved by _swap_math); tid_in is reloaded after the macros (they clobber T3).
    _load_be32(a, T3, S0, 4)                     # reload tid_in
    _pool_key(a, A3, T3, POOL_RTOK); a.sload_to(S3, A3)  # S3 = R_tok[A]
    a.add(T4, S3, S1); _guard_max(a, T4)         # R_tok[A] + amt_in <= MAX
    _pool_key(a, A3, T3, POOL_RBIS); a.sload_to(T6, A3)  # T6 = R_bis[A]
    _swap_math(a, S1, S3, T6, S2)                # S2 = bis_mid (out_dst=S2 distinct from r_in=S3, r_out=T6)
    a.beq(S2, ZERO, "revert")
    # update pool A: R_tok[A] += amt_in(S1) ; R_bis[A] -= bis_mid(S2)
    _load_be32(a, T3, S0, 4)                     # reload tid_in
    _pool_key(a, T5, T3, POOL_RTOK); a.sload_to(A4, T5); a.add(A4, A4, S1); a.sstore(T5, A4)
    _pool_key(a, T5, T3, POOL_RBIS); a.sload_to(A4, T5); a.sub(A4, A4, S2); a.sstore(T5, A4)

    # HOP 2: BIS bis_mid(S2) -> token tid_out.  out(S1) = swap(bis_mid; R_bis[B], R_tok[B]). bis_mid stays
    # in S2 (in_reg, preserved) for the R_bis[B] += bis_mid update below. out < R_tok[B] is guaranteed.
    _load_be32(a, T4, S0, 8)                     # tid_out
    _pool_key(a, A3, T4, POOL_RBIS); a.sload_to(S3, A3)  # S3 = R_bis[B]
    a.add(T5, S3, S2); _guard_max(a, T5)         # R_bis[B] + bis_mid <= MAX
    _pool_key(a, A3, T4, POOL_RTOK); a.sload_to(T6, A3)  # T6 = R_tok[B]
    _swap_math(a, S2, S3, T6, S1)                # S1 = out (out_dst=S1 distinct from r_in=S3, r_out=T6)
    a.beq(S1, ZERO, "revert")
    _load_be32(a, T0, S0, 16); a.bltu(S1, T0, "revert")  # slippage: out >= min_out
    # update pool B: R_bis[B] += bis_mid(S2) ; R_tok[B] -= out(S1)
    _load_be32(a, T4, S0, 8)                     # reload tid_out
    _pool_key(a, T5, T4, POOL_RBIS); a.sload_to(A4, T5); a.add(A4, A4, S2); a.sstore(T5, A4)
    _pool_key(a, T5, T4, POOL_RTOK); a.sload_to(A4, T5); a.sub(A4, A4, S1); a.sstore(T5, A4)
    # credit caller token tid_out += out(S1)
    _load_be32(a, T4, S0, 8)                     # reload tid_out
    _bal_key(a, T5, T4, T2); _credit(a, T5, S1)
    a.halt()

    # --- revert: illegal instruction -> deterministic revert (no commit, no transfer) ---------------
    a.label("revert")
    a.raw(0)

    return a.assemble()


# --- read helpers (tests / a relay reading /api/vm/contract storage) -------------------------------
def pool_key(tid, field):
    return TAG_POOL | ((tid & 0xF) << 4) | field


def balance_key(tid, party_id):
    return TAG_BAL | ((tid & 0xF) << 24) | (party_id & USER_MASK)


def lp_key(tid, party_id):
    return TAG_LP | ((tid & 0xF) << 24) | (party_id & USER_MASK)
