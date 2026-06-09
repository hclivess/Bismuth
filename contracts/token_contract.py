"""
token.py — a minimal fixed-supply fungible TOKEN (balances + transfer), as one hand-authored RV32I
contract for the Bismuth decentralized-apps VM (bismuth_riscv / vm_engine). Demonstrates pure on-chain
ACCOUNTING in contract storage — no BIS custody, an ERC20-in-miniature.

WHAT IT DOES
  A token whose entire supply is minted once to the owner, after which holders move balances between
  32-bit account ids (the VM's fold of a caller / address). Balances live in the contract's storage map;
  transfers debit the caller and credit the recipient with an overflow/underflow-safe check.

DEPLOY
  operation = "vm:deploy", openfield = build(owner_id, total_supply).hex()
    owner_id = low 32 bits of the owner's Bismuth address int; total_supply = 32-bit token amount.
    Use account_id_of(address).

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>")
  selector = first 4 bytes big-endian:
    FN_INIT     (0) — owner-only, once. Mints total_supply to the owner's token balance.
    FN_TRANSFER (1) — calldata = selector(4) | to_account_id(4 BE) | amount(4 BE). Moves `amount` tokens
                      from the caller's balance to `to_account_id`. Reverts on insufficient balance.
  (Balances are read off-chain via the contract's storage: key = TAG_BAL | (account_id & 0x0FFFFFFF).)

STORAGE (32-bit word slots)
  1 = initialised flag (0/1)
  0x4xxxxxxx = per-account token balance (key = 0x40000000 | (account_id & 0x0FFFFFFF))
"""
import hashlib

from asmtools import Asm, A0, A2, A3, A4, A5, S0, T0, T1, T2, T3, T4, ZERO

FN_INIT = 0
FN_TRANSFER = 1

S_INIT = 1
TAG_BAL = 0x40000000
ACC_MASK = 0x0FFFFFFF


def account_id_of(address):
    """Low 32 bits of a Bismuth address int — the VM's fold of CALLER / an address."""
    try:
        return int(str(address), 16) & 0xFFFFFFFF
    except Exception:
        return int.from_bytes(hashlib.blake2b(str(address).encode(), digest_size=32).digest(),
                              "big") & 0xFFFFFFFF


def balance_slot(account_id):
    """Storage key holding `account_id`'s token balance (for off-chain reads)."""
    return TAG_BAL | (account_id & ACC_MASK)


def _load_be32(a, rd, base_reg, off):
    a.lbu(rd, base_reg, off)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 1); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 2); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 3); a.or_(rd, rd, A2)


def build(owner_id, total_supply):
    """Assemble the token with owner id and total supply baked in."""
    a = Asm()
    a.mv(S0, A0)
    _load_be32(a, T0, S0, 0)                        # T0 = selector
    a.caller(T1)                                   # T1 = caller fold
    a.li(A5, ACC_MASK)
    a.and_(T2, T1, A5)                              # T2 = caller account suffix

    a.li(A2, FN_INIT);     a.beq(T0, A2, "init")
    a.li(A2, FN_TRANSFER); a.beq(T0, A2, "transfer")
    a.j("revert")

    # --- INIT: owner-only, once; mint supply to owner -------------------
    a.label("init")
    a.li(A2, owner_id); a.bne(T1, A2, "revert")
    a.li(A3, S_INIT); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")   # once
    a.li(A3, S_INIT); a.li(A4, 1); a.sstore(A3, A4)
    a.li(A3, TAG_BAL); a.or_(A3, A3, T2)           # owner balance slot
    a.li(A4, total_supply); a.sstore(A3, A4)
    a.halt()

    # --- TRANSFER: caller -> to_account, amount -------------------------
    a.label("transfer")
    _load_be32(a, T3, S0, 4)                        # T3 = to_account_id
    a.li(A5, ACC_MASK); a.and_(T3, T3, A5)         # to suffix
    _load_be32(a, T4, S0, 8)                        # T4 = amount
    a.beq(T4, ZERO, "revert")                       # zero transfer is a no-op revert
    # from balance
    a.li(A3, TAG_BAL); a.or_(A3, A3, T2); a.sload_to(A4, A3)          # A4 = from balance
    a.bltu(A4, T4, "revert")                        # insufficient -> revert
    a.sub(A4, A4, T4); a.sstore(A3, A4)            # debit sender (A3 still = from slot)
    # to balance
    a.li(A3, TAG_BAL); a.or_(A3, A3, T3); a.sload_to(A4, A3)          # A4 = to balance
    a.add(A4, A4, T4); a.sstore(A3, A4)            # credit recipient
    a.halt()

    a.label("revert")
    a.raw(0)
    return a.assemble()
