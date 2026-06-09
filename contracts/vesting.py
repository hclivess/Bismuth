"""
vesting.py — a height-locked VESTING / TIMELOCK vault, as one hand-authored RV32I contract for the
Bismuth decentralized-apps VM (bismuth_riscv / vm_engine). Demonstrates TIME/HEIGHT GATING via SYS_NUMBER.

WHAT IT DOES
  Anyone funds the vault with BIS. The funds stay locked until the chain reaches a target block height;
  from then on the beneficiary can withdraw the whole balance. Before the unlock height every withdraw
  reverts — the gate is the consensus block number, identical on every node.

DEPLOY
  operation = "vm:deploy", openfield = build(beneficiary_id, unlock_height).hex()
    beneficiary_id = low 32 bits of the beneficiary's Bismuth address int; unlock_height = absolute block
    height (32-bit) at/after which release is allowed. Use beneficiary_id_of(address).

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>")
  selector = first 4 bytes big-endian:
    FN_FUND    (0) — anyone; send BIS to VM_SINK with this call to add to the locked balance.
    FN_RELEASE (1) — calldata = selector(4) | beneficiary_recipient(28 BE). Reverts unless caller is the
                     beneficiary AND current block height >= unlock_height AND there is a balance. Pays
                     the whole balance to the supplied recipient and zeroes the vault.
  A revert commits nothing and transfers nothing.

STORAGE (32-bit word slots)
  1 = locked amount (units)
"""
import hashlib

from asmtools import Asm, A0, A2, A3, A4, S0, T0, T1, T2, T3, ZERO

FN_FUND = 0
FN_RELEASE = 1

S_AMOUNT = 1
SCRATCH = 4096


def beneficiary_id_of(address):
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


def build(beneficiary_id, unlock_height):
    """Assemble the vault with the beneficiary id and unlock height baked in."""
    a = Asm()
    a.mv(S0, A0)
    _load_be32(a, T0, S0, 0)                        # T0 = selector
    a.caller(T1)                                   # T1 = caller fold

    a.li(A2, FN_FUND);    a.beq(T0, A2, "fund")
    a.li(A2, FN_RELEASE); a.beq(T0, A2, "release")
    a.j("revert")

    # --- FUND: anyone; amount += callvalue ------------------------------
    a.label("fund")
    a.callvalue(T3); a.beq(T3, ZERO, "revert")
    a.li(A3, S_AMOUNT); a.sload_to(A4, A3); a.add(A4, A4, T3); a.sstore(A3, A4)
    a.halt()

    # --- RELEASE: beneficiary, at/after unlock height -------------------
    a.label("release")
    a.li(A2, beneficiary_id); a.bne(T1, A2, "revert")    # beneficiary only
    a.number(T2)                                          # T2 = current block height
    a.li(A2, unlock_height)
    a.bltu(T2, A2, "revert")                              # height < unlock -> still locked
    a.li(A3, S_AMOUNT); a.sload_to(T3, A3); a.beq(T3, ZERO, "revert")  # nothing to release
    a.li(A3, S_AMOUNT); a.sstore(A3, ZERO)               # zero the vault BEFORE transfer
    a.li(A4, SCRATCH); a.store_u64_be(T3, A4, 0)
    a.addi(A3, S0, 4)                                    # recipient ptr (calldata+4)
    a.transfer(A3, A4)
    a.halt()

    a.label("revert")
    a.raw(0)
    return a.assemble()
