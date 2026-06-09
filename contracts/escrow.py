"""
escrow.py — a buyer / seller / arbiter ESCROW, as one hand-authored RV32I contract for the Bismuth
decentralized-apps VM (bismuth_riscv / vm_engine). Demonstrates ACCESS CONTROL + value custody.

WHAT IT DOES
  The buyer deposits BIS into the contract. The funds are released to the SELLER on a RELEASE authorised
  by the buyer or the arbiter, or refunded to the BUYER on a REFUND authorised by the seller or the
  arbiter. Exactly one terminal action: once released or refunded, the contract is closed.

DEPLOY
  operation = "vm:deploy", openfield = build(buyer_id, seller_id, arbiter_id).hex()
    each *_id = low 32 bits of that party's Bismuth address int (the VM truncates CALLER to 32 bits),
    baked into the bytecode. Use party_id_of(address).

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>")
  selector = first 4 bytes big-endian:
    FN_DEPOSIT (0) — buyer-only; send BIS to VM_SINK with this call to fund the escrow (accumulates).
    FN_RELEASE (1) — calldata = selector(4) | seller_recipient(28 BE). Caller must be buyer OR arbiter,
                     and the escrow must be funded and still open. Pays the whole balance to the seller.
    FN_REFUND  (2) — calldata = selector(4) | buyer_recipient(28 BE). Caller must be seller OR arbiter,
                     and funded and still open. Pays the whole balance back to the buyer.
  A revert commits nothing and transfers nothing.

STORAGE (32-bit word slots)
  1 = amount held (units, accumulated by DEPOSIT)     2 = closed flag (0 open / 1 released/refunded)
"""
import hashlib

from asmtools import Asm, A0, A2, A3, A4, S0, T0, T1, T3, ZERO

FN_DEPOSIT = 0
FN_RELEASE = 1
FN_REFUND = 2

S_AMOUNT = 1
S_CLOSED = 2
SCRATCH = 4096


def party_id_of(address):
    """Low 32 bits of a Bismuth address int — the VM's 32-bit fold of CALLER."""
    try:
        return int(str(address), 16) & 0xFFFFFFFF
    except Exception:
        return int.from_bytes(hashlib.blake2b(str(address).encode(), digest_size=32).digest(),
                              "big") & 0xFFFFFFFF


def _load_be32(a, rd, base_reg, off):
    a.lbu(rd, base_reg, off)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 1); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 2); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 3); a.or_(rd, rd, A2)


def build(buyer_id, seller_id, arbiter_id):
    """Assemble the escrow with the three party ids baked in."""
    a = Asm()
    a.mv(S0, A0)                                   # stash calldata ptr (syscalls clobber a0)
    _load_be32(a, T0, S0, 0)                        # T0 = selector
    a.caller(T1)                                   # T1 = caller fold

    a.li(A2, FN_DEPOSIT); a.beq(T0, A2, "deposit")
    a.li(A2, FN_RELEASE); a.beq(T0, A2, "release")
    a.li(A2, FN_REFUND);  a.beq(T0, A2, "refund")
    a.j("revert")

    # --- DEPOSIT: buyer-only, while open; amount += callvalue ------------
    a.label("deposit")
    a.li(A2, buyer_id); a.bne(T1, A2, "revert")
    a.li(A3, S_CLOSED); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")
    a.callvalue(T3); a.beq(T3, ZERO, "revert")     # must attach value
    a.li(A3, S_AMOUNT); a.sload_to(A4, A3); a.add(A4, A4, T3); a.sstore(A3, A4)
    a.halt()

    # --- RELEASE: buyer OR arbiter -> pay all to seller -----------------
    a.label("release")
    a.li(A2, buyer_id); a.beq(T1, A2, "rel_ok")
    a.li(A2, arbiter_id); a.beq(T1, A2, "rel_ok")
    a.j("revert")
    a.label("rel_ok")
    a.j("payout")                                  # recipient = calldata+4 (caller supplies seller addr)

    # --- REFUND: seller OR arbiter -> pay all to buyer ------------------
    a.label("refund")
    a.li(A2, seller_id); a.beq(T1, A2, "ref_ok")
    a.li(A2, arbiter_id); a.beq(T1, A2, "ref_ok")
    a.j("revert")
    a.label("ref_ok")
    a.j("payout")

    # --- shared payout: require funded & open, then transfer all -------
    a.label("payout")
    a.li(A3, S_CLOSED); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")   # already closed
    a.li(A3, S_AMOUNT); a.sload_to(T3, A3); a.beq(T3, ZERO, "revert")   # nothing funded
    a.li(A3, S_CLOSED); a.li(A4, 1); a.sstore(A3, A4)                   # close BEFORE transfer
    a.li(A4, SCRATCH); a.store_u64_be(T3, A4, 0)                         # 8-byte BE amount
    a.addi(A3, S0, 4)                              # recipient ptr (calldata+4, 28 bytes)
    a.transfer(A3, A4)
    a.halt()

    a.label("revert")
    a.raw(0)
    return a.assemble()
