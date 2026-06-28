"""
ownable.py — an OPT-IN ownership module for Bismuth VM contracts (doc/44 §Ownership).

Ownership is an application convention, not an engine feature (the EVM has no built-in owner either —
`Ownable` is a library). This module packages the convention so any hand-authored RV32I contract can splice
in a standard, audited owner model:

  * OPTIONAL & IMMUTABLE BY DEFAULT — a contract that doesn't use this module has no owner and no admin
    surface at all. You opt in by emitting the helpers below.
  * BOUND AT INCEPTION — the initial owner is baked into the bytecode at deploy (`build(owner_addr, ...)`),
    so the contract is owned by a chosen account from block 0. No init call is needed.
  * TRANSFERABLE (two-step) — the owner PROPOSEs a new owner; the new owner must ACCEPT. Two-step prevents
    bricking admin by transferring to a typo'd / keyless address (the OpenZeppelin Ownable2Step stance).
  * DELEGABLE — the owner can bind a revocable DELEGATE account that may exercise OPERATIONAL powers
    (whatever the contract gates with `require_owner_or_delegate`) without being able to transfer or
    renounce ownership itself.
  * RENOUNCEABLE — the owner can RENOUNCE, making the contract permanently ownerless / immutable (a one-way
    switch). The explicit "make it immutable now" action.

IDENTITY IS THE FULL 224-BIT ADDRESS. Owner/delegate checks read the caller via `SYS_CALLER_FULL` (28
bytes) and compare all 28 bytes, NOT the 32-bit `SYS_CALLER` fold — the fold is only ~2^32-strong and is
grindable, which is unacceptable for ownership that gates upgrades / value. Owners/delegates are stored as
seven 32-bit words.

RESERVED STORAGE SLOTS — an owned contract MUST NOT use slots 0xFFFF0000..0xFFFF001F for anything else.
RESERVED CALL SELECTORS — 0xF0010000..0xF0010003 are the ownership admin functions; the contract's own
selectors must avoid them (the demos use small selectors like 0/1, so there is no clash).

This is TOOLING (it emits bytecode); nodes execute the assembled bytecode, never this module.
"""
from asmtools import (Asm, A0, A2, A3, ZERO, S0, T0, T1, T2, T3, T4, T5, T6,
                      SYS_CALLER_FULL)

# --- reserved storage slots (7-word = 28-byte address fields) ----------------------------------------
S_OWNER_SET = 0xFFFF0000          # 0 = effective owner is the baked initial owner; 1 = it is in S_OWNER
S_OWNER = 0xFFFF0001              # .. +6   current owner (after a transfer)
S_PEND_SET = 0xFFFF0008           # 1 = a pending owner is proposed
S_PENDING = 0xFFFF0009            # .. +6   proposed owner (two-step transfer)
S_DGT_SET = 0xFFFF0010            # 1 = a delegate is set
S_DELEGATE = 0xFFFF0011           # .. +6   delegate account
S_RENOUNCED = 0xFFFF0018          # 1 = ownerless / immutable forever

# --- reserved admin selectors (calldata = selector(4 BE) | arg) --------------------------------------
OWN_TRANSFER = 0xF0010000         # owner: propose a new owner.   calldata = sel(4) | new_owner(28 BE)
OWN_ACCEPT = 0xF0010001           # pending owner: accept ownership
OWN_RENOUNCE = 0xF0010002         # owner: renounce -> ownerless/immutable forever
OWN_SET_DELEGATE = 0xF0010003     # owner: set/clear delegate.    calldata = sel(4) | delegate(28 BE), 0 clears

# --- scratch memory (high, clear of code + calldata for these contracts) -----------------------------
_M_CALLER = 0x4000                # 28-byte caller
_M_OWNER = 0x4020                 # 28-byte effective owner / comparison scratch
_M_ARG = 0x4040                   # 28-byte argument (proposed owner / delegate) copied from calldata


# ============================ low-level 28-byte (7-word) helpers ============================
# Addresses are handled as 7 opaque 4-byte chunks. `lw`/`sw` move chunks verbatim (byte-identical), so the
# big-endian layout written by SYS_CALLER_FULL / calldata round-trips through storage and compares correctly
# without any endianness juggling — equality is all ownership needs.
def _imm28_to_mem(a, val_int, mem_base):
    a.li(T1, mem_base)
    bs = (val_int & ((1 << 224) - 1)).to_bytes(28, "big")
    for i in range(7):
        a.li(T0, int.from_bytes(bs[4 * i:4 * i + 4], "little"))   # sw writes little-endian -> BE bytes land
        a.sw(T0, T1, 4 * i)


def _storage28_to_mem(a, s_base, mem_base):
    a.li(T1, mem_base)
    for i in range(7):
        a.li(T2, s_base + i); a.sload_to(T0, T2); a.sw(T0, T1, 4 * i)


def _mem28_to_storage(a, mem_base, s_base):
    a.li(T1, mem_base)
    for i in range(7):
        a.lw(T0, T1, 4 * i); a.li(T2, s_base + i); a.sstore(T2, T0)


def _clear28(a, s_base):
    """Zero all 7 words of a 28-byte storage field (vm_state deletes 0-slots, so this also frees state)."""
    for i in range(7):
        a.li(T2, s_base + i); a.sstore(T2, ZERO)


def _cd28_to_mem(a, cd_base_reg, offset, mem_base):
    a.addi(T1, cd_base_reg, offset)
    a.li(T2, mem_base)
    for i in range(7):
        a.lw(T0, T1, 4 * i); a.sw(T0, T2, 4 * i)


def _memcmp28_neq(a, base_a, base_b, neq_label):
    """Branch to neq_label if the two 28-byte buffers differ; fall through if equal."""
    a.li(T2, base_a); a.li(T3, base_b)
    for i in range(7):
        a.lw(T0, T2, 4 * i); a.lw(T1, T3, 4 * i); a.bne(T0, T1, neq_label)


def _memcmp28_eq(a, base_a, base_b, eq_label):
    """Branch to eq_label if the two 28-byte buffers are equal; fall through if they differ."""
    ne = a._uniq("cmp_ne")
    a.li(T2, base_a); a.li(T3, base_b)
    for i in range(7):
        a.lw(T0, T2, 4 * i); a.lw(T1, T3, 4 * i); a.bne(T0, T1, ne)
    a.j(eq_label)
    a.label(ne)


def _mem28_nonzero(a, base, nonzero_label):
    """Branch to nonzero_label if any word of the 28-byte buffer is non-zero; fall through if all zero."""
    a.li(T2, base)
    for i in range(7):
        a.lw(T0, T2, 4 * i); a.bne(T0, ZERO, nonzero_label)


def _load_effective_owner(a, baked_owner_int, out_mem):
    """Write the effective owner (28 bytes) to out_mem: the stored owner if a transfer ever happened, else
    the baked initial owner. (Caller must already have handled the renounced case.)"""
    baked = a._uniq("ow_baked"); done = a._uniq("ow_done")
    a.li(T4, S_OWNER_SET); a.sload_to(T5, T4); a.beq(T5, ZERO, baked)
    _storage28_to_mem(a, S_OWNER, out_mem)
    a.j(done)
    a.label(baked)
    _imm28_to_mem(a, baked_owner_int, out_mem)
    a.label(done)


# ============================ public emit helpers ============================
def emit_require_owner(a, baked_owner_int, not_owner_label):
    """Revert (jump to not_owner_label) unless the caller is the current owner. Falls through if owner.
    Renounced => never owner. Clobbers T0-T6, A0-A3, A7; uses memory _M_CALLER/_M_OWNER; preserves S0-S3."""
    a.li(T6, _M_CALLER); a.caller_full(T6)
    a.li(T4, S_RENOUNCED); a.sload_to(T5, T4); a.bne(T5, ZERO, not_owner_label)
    _load_effective_owner(a, baked_owner_int, _M_OWNER)
    _memcmp28_neq(a, _M_CALLER, _M_OWNER, not_owner_label)


def emit_require_owner_or_delegate(a, baked_owner_int, not_owner_label):
    """Revert unless the caller is the current owner OR the set delegate. Falls through if allowed."""
    ok = a._uniq("od_ok")
    a.li(T6, _M_CALLER); a.caller_full(T6)
    a.li(T4, S_RENOUNCED); a.sload_to(T5, T4); a.bne(T5, ZERO, not_owner_label)
    _load_effective_owner(a, baked_owner_int, _M_OWNER)
    _memcmp28_eq(a, _M_CALLER, _M_OWNER, ok)                 # owner -> ok
    a.li(T4, S_DGT_SET); a.sload_to(T5, T4); a.beq(T5, ZERO, not_owner_label)   # no delegate -> fail
    _storage28_to_mem(a, S_DELEGATE, _M_OWNER)
    _memcmp28_neq(a, _M_CALLER, _M_OWNER, not_owner_label)   # delegate must match
    a.label(ok)


def emit_admin_dispatch(a, baked_owner_int, selector_reg, cd_base_reg, fallthrough_label):
    """Handle the four ownership admin selectors (TRANSFER/ACCEPT/RENOUNCE/SET_DELEGATE). If `selector_reg`
    is none of them, jump to `fallthrough_label` (the contract's own dispatch). Emit this near the top of a
    contract's dispatch. `cd_base_reg` must hold the calldata pointer (a0 on entry)."""
    u = a._uniq("admin")
    L_tr, L_ac, L_re, L_dg = ("own_tr" + u), ("own_ac" + u), ("own_re" + u), ("own_dg" + u)
    L_rev = "own_rev" + u

    a.li(A2, OWN_TRANSFER);     a.beq(selector_reg, A2, L_tr)
    a.li(A2, OWN_ACCEPT);       a.beq(selector_reg, A2, L_ac)
    a.li(A2, OWN_RENOUNCE);     a.beq(selector_reg, A2, L_re)
    a.li(A2, OWN_SET_DELEGATE); a.beq(selector_reg, A2, L_dg)
    a.j(fallthrough_label)

    # TRANSFER (owner only): stage calldata[4:32] as the pending owner; must be non-zero.
    a.label(L_tr)
    emit_require_owner(a, baked_owner_int, L_rev)
    _cd28_to_mem(a, cd_base_reg, 4, _M_ARG)
    nz = a._uniq("tr_nz")
    _mem28_nonzero(a, _M_ARG, nz)                            # non-zero -> ok
    a.j(L_rev)                                               # all-zero proposed owner -> revert (use RENOUNCE)
    a.label(nz)
    _mem28_to_storage(a, _M_ARG, S_PENDING)
    a.li(A2, S_PEND_SET); a.li(A3, 1); a.sstore(A2, A3)
    a.li(A0, 1); a.ret()

    # ACCEPT (pending owner only): promote the pending owner to owner.
    a.label(L_ac)
    a.li(A2, S_PEND_SET); a.sload_to(A3, A2); a.beq(A3, ZERO, L_rev)
    a.li(T6, _M_CALLER); a.caller_full(T6)
    _storage28_to_mem(a, S_PENDING, _M_OWNER)
    _memcmp28_neq(a, _M_CALLER, _M_OWNER, L_rev)             # caller must be the pending owner
    _mem28_to_storage(a, _M_OWNER, S_OWNER)                  # owner := pending
    a.li(A2, S_OWNER_SET); a.li(A3, 1); a.sstore(A2, A3)
    a.li(A2, S_PEND_SET); a.sstore(A2, ZERO)
    _clear28(a, S_PENDING)                                   # drop the now-stale pending address
    a.li(A0, 1); a.ret()

    # RENOUNCE (owner only): ownerless / immutable forever; clear pending + delegate (flags AND data).
    a.label(L_re)
    emit_require_owner(a, baked_owner_int, L_rev)
    a.li(A2, S_RENOUNCED); a.li(A3, 1); a.sstore(A2, A3)
    a.li(A2, S_PEND_SET); a.sstore(A2, ZERO)
    a.li(A2, S_DGT_SET); a.sstore(A2, ZERO)
    _clear28(a, S_PENDING)
    _clear28(a, S_DELEGATE)
    a.li(A0, 1); a.ret()

    # SET_DELEGATE (owner only): set, or clear when the argument is all-zero.
    a.label(L_dg)
    emit_require_owner(a, baked_owner_int, L_rev)
    _cd28_to_mem(a, cd_base_reg, 4, _M_ARG)
    set_it = a._uniq("dg_set"); dg_done = a._uniq("dg_done")
    _mem28_nonzero(a, _M_ARG, set_it)                       # non-zero -> set; zero -> clear
    a.li(A2, S_DGT_SET); a.sstore(A2, ZERO)
    _clear28(a, S_DELEGATE)                                 # drop the now-stale delegate address
    a.j(dg_done)
    a.label(set_it)
    _mem28_to_storage(a, _M_ARG, S_DELEGATE)
    a.li(A2, S_DGT_SET); a.li(A3, 1); a.sstore(A2, A3)
    a.label(dg_done)
    a.li(A0, 1); a.ret()

    a.label(L_rev)
    a.raw(0)


# ============================ standalone demo: an owned counter ============================
# BUMP is owner-or-delegate gated; GET is public. Proves the module end-to-end on its own.
FN_BUMP = 0
FN_GET = 1
APP_SLOT = 0x100      # application state, clear of the 0xFFFF.... reserved range


def _ld_be32(a, rd, base, off, scr=T6):
    a.lbu(rd, base, off)
    a.slli(rd, rd, 8); a.lbu(scr, base, off + 1); a.or_(rd, rd, scr)
    a.slli(rd, rd, 8); a.lbu(scr, base, off + 2); a.or_(rd, rd, scr)
    a.slli(rd, rd, 8); a.lbu(scr, base, off + 3); a.or_(rd, rd, scr)


def owned_counter(owner_addr_hex):
    """Demo owned contract: BUMP (owner-or-delegate) increments slot APP_SLOT; GET (public) reads it; the
    standard ownership admin (TRANSFER/ACCEPT/RENOUNCE/SET_DELEGATE) comes from ownable."""
    owner_int = int(owner_addr_hex, 16)
    a = Asm()
    a.mv(S0, A0)                                            # calldata base
    _ld_be32(a, T0, S0, 0)                                  # selector
    emit_admin_dispatch(a, owner_int, T0, S0, "app")        # OWN_* handled here, else fall to "app"
    a.label("app")
    a.li(A2, FN_BUMP); a.beq(T0, A2, "bump")
    a.li(A2, FN_GET);  a.beq(T0, A2, "get")
    a.raw(0)                                                # unknown selector -> revert
    a.label("bump")
    emit_require_owner_or_delegate(a, owner_int, "rev")
    a.li(A3, APP_SLOT); a.sload_to(T1, A3); a.addi(T1, T1, 1); a.sstore(A3, T1)
    a.mv(A0, T1); a.ret()
    a.label("get")
    a.li(A3, APP_SLOT); a.sload_to(A0, A3); a.ret()
    a.label("rev"); a.raw(0)
    return a.assemble()
