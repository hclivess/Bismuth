"""
bridge_vault.py — the peg-IN custody vault as one hand-authored RV32I contract for the Bismuth VM
(doc/45 bridge). A user LOCKS BIS here, naming the Ethereum address that should receive the wrapped wBIS.
The lock is recorded in contract storage so it is **Merkle-provable** (vm_state.merkle_prove_storage)
against the committed VM state root (doc/45 Stage 2b) — exactly what an Ethereum-side verifier (Stage 3,
zk) checks before minting wBIS. No operator, no admin key: the BIS sits in the contract's own custody and
the record is in consensus state.

DEPLOY
  operation = "vm:deploy", openfield = build().hex()

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>", value = BIS to lock)
  FN_LOCK (the only entry): calldata = eth_recipient(20 bytes); attach the BIS to lock as the call value.
    Effect: n = ++lock_counter; record the lock at storage base = n*8:
      slot[n*8 + 0]      = amount (the locked BIS units)
      slot[n*8 + 1 .. 5] = the 20-byte Ethereum recipient, as 5 big-endian 32-bit words
    The attached value stays in the vault's custody (real locked BIS). Returns n (the lock id).
    A peg-out / refund path (release on a verified ETH burn proof) builds on eth_verify.py + the MPT/finality
    checks — see doc/45; this contract is the custody + provable-record half.

LIMITATION (documented): the VM exposes callvalue and storage values as 32-bit words, so a single lock's
amount is bounded by 2**32 units in the VM's view (the custody balance itself is full-width). doc/45 notes
widening this (multi-word amounts) alongside the other Stage-2/3 work.

Hand-authored RV32I via asmtools (same idiom as escrow.py / bridge eth_verify.py).
"""
from asmtools import Asm, A0, A2, A3, A4, S0, T0, T1, T3, ZERO

S_COUNTER = 0    # storage slot holding the running lock count


def _load_be32(a, rd, base_reg, off):
    """rd = the big-endian 32-bit word at [base_reg + off] (clobbers A2)."""
    a.lbu(rd, base_reg, off)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 1); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 2); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 3); a.or_(rd, rd, A2)


def build():
    """Assemble the peg-in vault. No parameters — every lock is recorded under an incrementing id."""
    a = Asm()
    a.mv(S0, A0)                                   # calldata ptr (eth_recipient, 20 bytes)
    a.callvalue(T0)                                # T0 = amount to lock
    a.beq(T0, ZERO, "revert")                      # must lock a positive amount

    a.li(A3, S_COUNTER); a.sload_to(T1, A3)        # T1 = current lock count
    a.addi(T1, T1, 1)                              # T1 = n (this lock's id)
    a.li(A3, S_COUNTER); a.sstore(A3, T1)          # persist the new count

    a.slli(T3, T1, 3)                              # T3 = base = n * 8
    a.sstore(T3, T0)                               # slot[base+0] = amount

    for i in range(5):                             # slot[base+1..5] = eth_recipient, 5 big-endian words
        _load_be32(a, T0, S0, i * 4)
        a.addi(A4, T3, 1 + i)
        a.sstore(A4, T0)

    a.mv(A0, T1)                                   # return the lock id n
    a.ret()

    a.label("revert")
    a.raw(0)
    return a.assemble()
