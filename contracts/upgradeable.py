"""
upgradeable.py — the canonical UPGRADEABLE-CONTRACT pattern for the Bismuth VM (doc/19 — issue #384),
hand-authored RV32I. A thin **proxy** at a fixed address owns the storage + custody and holds a swappable
**implementation address**; every ordinary call is ``SYS_DELEGATECALL``ed into the current implementation,
which therefore reads and writes the *proxy's* slots. You "upgrade" by an owner-gated write of a new impl
address — the address, storage and balance stay put; only the logic moves. This is the EVM proxy pattern,
made possible by the new ``DELEGATECALL`` syscall.

WHY A PROXY (vs. SYS_SETCODE): SETCODE lets a contract replace its OWN code in place (see the owner-gated
self-upgrade in the docs/tests) — simplest when one contract upgrades itself. A proxy instead keeps the
logic in a SEPARATE implementation contract and swaps a pointer, so the implementation can be audited and
shared, and a buggy impl is reverted by repointing rather than overwriting. Both are supported; pick per
contract.

OWNERSHIP is provided by contracts/ownable.py on the FULL 224-bit address (collision-resistant), and is
transferable (two-step), delegable, and renounceable. INIT is owner-only; UPGRADE is owner-OR-delegate.

DEPLOY
  proxy:  operation="vm:deploy", openfield = build(owner_addr, impl_v1_addr).hex()   (owner = full 56-hex)
  impls:  operation="vm:deploy", openfield = impl_v1().hex()   (and later impl_v2().hex())

CALL ABI (operation="vm:call", openfield="<proxy_addr>:<calldata_hex>"), selector = first 4 bytes BE:
  ADMIN_INIT    (0xF0000000) — owner-only, once. Writes the baked initial impl into the impl slots.
  ADMIN_UPGRADE (0xF0000001) — owner OR delegate. calldata = selector(4) | new_impl(28 BE). Repoints it.
  OWN_* (0xF0010000..3)      — ownership admin (transfer/accept/renounce/set-delegate); see ownable.py.
  <anything else>            — DELEGATECALL the current impl, forwarding the WHOLE calldata + value-context.

STORAGE LAYOUT (the proxy OWNS these; an implementation MUST NOT use slots 0..7 nor ownable's reserved
0xFFFF0000..0xFFFF001F range — the classic proxy storage-collision hazard. Implementations here keep
their state at slot >= APP_BASE = 0x100):
  0      S_INIT   — initialised flag (0/1)
  1..7   S_IMPL0..6 — the 28-byte (7-word, big-endian) implementation address
  0xFFFF0000.. — ownable's owner/pending/delegate/renounced slots
"""
import ownable as own
from asmtools import Asm, A0, A1, A2, A3, A4, S0, S1, T0, T1, T2, T3, T4, T6, ZERO

ADMIN_INIT = 0xF0000000
ADMIN_UPGRADE = 0xF0000001
# Ownership (transfer/accept/renounce/delegate) is provided by contracts/ownable.py on the FULL 224-bit
# address. INIT binds the impl (owner only); UPGRADE repoints it (owner OR delegate — an operational power).

S_INIT = 0
S_IMPL0 = 1            # impl address occupies slots 1..7 (7 words * 4 bytes = 28 bytes)
APP_BASE = 0x100       # implementations put their own state here, clear of the proxy control slots

# scratch memory the proxy uses (well past code+calldata for these small demos)
_IMPL_BUF = 256        # 28-byte impl address assembled here for SYS_DELEGATECALL
_RET_BUF = 512


def _ld_be32(a, rd, base, off, scr=T6):
    a.lbu(rd, base, off)
    a.slli(rd, rd, 8); a.lbu(scr, base, off + 1); a.or_(rd, rd, scr)
    a.slli(rd, rd, 8); a.lbu(scr, base, off + 2); a.or_(rd, rd, scr)
    a.slli(rd, rd, 8); a.lbu(scr, base, off + 3); a.or_(rd, rd, scr)


def _st_be32(a, val, base, off, scr=T6):
    for i in range(4):
        a.srli(scr, val, (3 - i) * 8)
        a.sb(scr, base, off + i)


def build(owner_addr_hex, impl_hex):
    """Assemble the proxy bound to `owner_addr_hex` (a full 56-hex owner address) with the INITIAL
    implementation baked in. Ownership is transferable/delegable/renounceable via the ownable admin
    selectors (full-address identity)."""
    owner_int = int(owner_addr_hex, 16)
    impl_int = int(impl_hex, 16)
    a = Asm()
    a.mv(S0, A0); a.mv(S1, A1)                       # save the original calldata ptr/len
    _ld_be32(a, T0, S0, 0)                            # selector
    own.emit_admin_dispatch(a, owner_int, T0, S0, "proxy")   # OWN_* admin handled here, else fall through

    a.label("proxy")
    a.li(A2, ADMIN_INIT);    a.beq(T0, A2, "init")
    a.li(A2, ADMIN_UPGRADE); a.beq(T0, A2, "upgrade")
    a.j("forward")

    # --- INIT: owner-only, once; bake the initial impl into the impl slots -----------------------
    a.label("init")
    own.emit_require_owner(a, owner_int, "revert")
    a.li(A3, S_INIT); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")      # once
    a.li(A3, S_INIT); a.li(A4, 1); a.sstore(A3, A4)
    for w in range(7):
        word = (impl_int >> ((6 - w) * 32)) & 0xFFFFFFFF
        a.li(A3, S_IMPL0 + w); a.li(A4, word); a.sstore(A3, A4)
    a.li(A0, 1); a.ret()

    # --- UPGRADE: owner OR delegate; calldata = selector(4) | new_impl(28 BE) ---------------------
    a.label("upgrade")
    own.emit_require_owner_or_delegate(a, owner_int, "revert")
    a.li(A3, S_INIT); a.sload_to(A4, A3); a.beq(A4, ZERO, "revert")      # must be initialised first
    for w in range(7):
        _ld_be32(a, A4, S0, 4 + w * 4)               # new impl word w from calldata[4 + 4w]
        a.li(A3, S_IMPL0 + w); a.sstore(A3, A4)
    a.li(A0, 1); a.ret()

    # --- FORWARD: DELEGATECALL the current impl with the full original calldata -------------------
    a.label("forward")
    a.li(A3, S_INIT); a.sload_to(A4, A3); a.beq(A4, ZERO, "revert")      # not initialised -> revert
    a.li(T2, _IMPL_BUF)
    for w in range(7):                               # assemble the 28-byte impl address into memory
        a.li(A3, S_IMPL0 + w); a.sload_to(A4, A3)
        _st_be32(a, A4, T2, w * 4)
    a.li(T3, _RET_BUF); a.li(T4, 4)
    a.delegatecall(T2, S0, S1, T3, T4)               # impl runs in OUR storage/custody/identity; a0=success
    a.beq(A0, ZERO, "revert")                        # a failed impl call reverts the proxy call too
    a.li(T0, _RET_BUF); _ld_be32(a, A0, T0, 0)       # return whatever the impl returned
    a.ret()

    a.label("revert"); a.raw(0)
    return a.assemble()


# ----------------------------------------------------------------- example implementations
# Both keep their single state word at APP_BASE (clear of the proxy's slots 0..7). v1 bumps the counter by
# 1; v2 — the "upgrade" — bumps by 10. Deployed STANDALONE and only ever reached through the proxy's
# DELEGATECALL, so the counter they read/write is the PROXY's storage, which survives the upgrade.
FN_BUMP = 0      # selector(4): counter += step; returns the new counter
FN_GET = 1       # selector(4): returns the current counter


def _impl(step):
    a = Asm()
    a.mv(S0, A0)
    _ld_be32(a, T0, S0, 0)                            # selector
    a.li(A2, FN_BUMP); a.beq(T0, A2, "bump")
    a.li(A2, FN_GET);  a.beq(T0, A2, "get")
    a.raw(0)                                          # unknown selector -> revert
    a.label("bump")
    a.li(A3, APP_BASE); a.sload_to(A4, A3)
    a.li(T1, step); a.add(A4, A4, T1)
    a.sstore(A3, A4)
    a.mv(A0, A4); a.ret()
    a.label("get")
    a.li(A3, APP_BASE); a.sload_to(A0, A3); a.ret()
    return a.assemble()


def impl_v1():
    """v1: BUMP adds 1."""
    return _impl(1)


def impl_v2():
    """v2 (the upgrade): BUMP adds 10. Same storage slot (APP_BASE), new behaviour."""
    return _impl(10)
