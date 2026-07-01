"""
bridge_vault.py — the peg-IN custody vault as one hand-authored RV32I contract for the Bismuth VM
(doc/45 bridge). A user LOCKS BIS here, naming the Ethereum address that should receive the wrapped wBIS.
The lock is recorded in contract storage so it is **Merkle-provable** (vm_state.merkle_prove_storage)
against the committed VM state root (doc/45 Stage 2b) — exactly what an Ethereum-side verifier (BismuthBridge
/ Stage 3 zk) checks before minting wBIS. No operator, no admin key: the BIS sits in the contract's own
custody and the record is in consensus state.

DEPLOY
  operation = "vm:deploy", openfield = build().hex()

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>", value = BIS to lock)
  FN_LOCK (the only entry): calldata = eth_recipient(20 bytes); attach the BIS to lock as the call value.
    Effect: n = ++lock_counter; record the lock at storage base = n*16:
      slot[n*16 + 0]       = amount (the locked BIS units, > 0)
      slot[n*16 + 1 .. 10] = the 20-byte Ethereum recipient as TEN 16-bit big-endian chunks, each stored as
                             (0x10000 | chunk16) — i.e. with a sentinel bit 16 set.
    The attached value stays in the vault's custody (real locked BIS). Returns n (the lock id).
    A peg-out / refund path (release on a verified ETH burn proof) builds on eth_verify.py + the MPT/finality
    checks — see doc/45; this contract is the custody + provable-record half.

WHY THE 0x10000 SENTINEL (do not remove): vm_state.commit_storage stores a slot value of 0 as a DELETION
(0 == unset, kept compact). A recipient whose 16-bit chunk is 0x0000 would therefore vanish from state and be
UNPROVABLE — the Ethereum verifier requires a valid inclusion proof for EVERY recipient chunk (skipping one
would let a forged claim redirect the lock), so an all-zero chunk would strand the locked BIS forever. Storing
(0x10000 | chunk16) makes every recorded value land in [0x10000, 0x1FFFF] — never zero — so EVERY recipient
(incl. leading-zero / vanity addresses) is always fully provable. The verifier strips the sentinel: chunk16 =
value & 0xFFFF, and checks (value >> 16) == 1. (16-bit chunks, not 32-bit words, because a single VM slot value
is at most 32 bits and any 32-bit value can be 0; a chunk smaller than the slot leaves room for the sentinel.)

LIMITATION (documented): the VM exposes callvalue and storage values as 32-bit words, so a single lock's
amount is bounded by 2**32 units in the VM's view (the custody balance itself is full-width). doc/45 notes
widening this (multi-word amounts) alongside the other Stage-2/3 work.

Hand-authored RV32I via asmtools (same idiom as escrow.py / bridge eth_verify.py).
"""
from asmtools import Asm, A0, A2, A3, A4, S0, T0, T1, T3, ZERO

S_COUNTER = 0    # storage slot holding the running lock count
SENTINEL = 0x10000   # bit 16 — guarantees every recipient chunk slot is non-zero (always provable)


def _load_be16(a, rd, base_reg, off):
    """rd = the big-endian 16-bit halfword at [base_reg + off] (clobbers A2)."""
    a.lbu(rd, base_reg, off)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 1); a.or_(rd, rd, A2)


def build():
    """Assemble the peg-in vault. No parameters — every lock is recorded under an incrementing id."""
    a = Asm()
    a.mv(S0, A0)                                   # calldata ptr (eth_recipient, 20 bytes)
    a.callvalue(T0)                                # T0 = amount to lock
    a.beq(T0, ZERO, "revert")                      # must lock a positive amount

    a.li(A3, S_COUNTER); a.sload_to(T1, A3)        # T1 = current lock count
    a.addi(T1, T1, 1)                              # T1 = n (this lock's id)
    a.li(A3, S_COUNTER); a.sstore(A3, T1)          # persist the new count

    a.slli(T3, T1, 4)                              # T3 = base = n * 16
    a.sstore(T3, T0)                               # slot[base+0] = amount (> 0, always provable)

    for j in range(10):                            # slot[base+1..10] = eth_recipient, 10 sentinel'd 16-bit chunks
        _load_be16(a, T0, S0, j * 2)               # T0 = recipient[2j:2j+2] big-endian (0..0xFFFF)
        a.li(A2, SENTINEL); a.or_(T0, T0, A2)      # T0 = 0x10000 | chunk  -> always in [0x10000, 0x1FFFF]
        a.addi(A4, T3, 1 + j)
        a.sstore(A4, T0)

    a.mv(A0, T1)                                   # return the lock id n
    a.ret()

    a.label("revert")
    a.raw(0)
    return a.assemble()
