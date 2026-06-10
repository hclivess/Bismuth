"""
dex.py — a decentralized exchange (limit order book) as one hand-authored RV32I contract for the Bismuth
in-core VM (bismuth_riscv / vm_engine). Trustless BIS <-> token trading with no operator and no custodian:
the contract escrows both sides and settles atomically, deposits credit its VM custody, payouts/refunds
leave via SYS_TRANSFER (consensus negative-height ledger rows). This is the marketplace primitive behind
escrow trades, OTC deals and token sales — and the on-chain counterpart to off-chain order books.

WHAT IT DOES
  The contract issues ONE fixed-supply token (genesis-minted to the deployer) and runs an order book that
  trades that token against native BIS. Anyone can:
    * post a SELL order  — lock `token_amt` token, ask `price_bis` BIS for it;
    * post a BUY  order  — lock `price_bis` BIS (attached as callvalue), offer it for `token_amt` token;
    * FILL an open order — the taker supplies the other side and settlement is atomic;
    * CANCEL their own open order — the escrow is refunded.
  Token balances are an internal ledger in contract storage; BIS lives in the contract's VM custody.
  Settlement moves BOTH legs or neither (a failed require reverts: no storage commit, no transfer).

DEPLOY  (operation = "vm:deploy", openfield = build(admin_id, total_supply).hex())
  admin_id     = low 32 bits of the deployer's address int (party_id_of) — the only account that can mint.
  total_supply = the fixed token supply (<= 2^32-1 units), credited to the admin by FN_INIT (once).

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>"); first 4 bytes = selector:
  FN_INIT     (0)  — admin-only, once: credit the whole token supply to the admin's balance.
  FN_TRANSFER (1)  — calldata: sel | to(28 BE) | amt(4 BE). Move `amt` token from caller to `to`.
  FN_SELL     (2)  — calldata: sel | maker(28 BE) | token_amt(4) | price_bis(4). Lock token, list for BIS.
  FN_BUY      (3)  — calldata: sel | maker(28 BE) | token_amt(4) | price_bis(4); ATTACH price_bis BIS as
                     value. Lock BIS in custody, bid for token.   (maker(28) must be the caller's address.)
  FN_FILL     (4)  — calldata: sel | order_id(4) | taker(28 BE). Fill an open order:
                       SELL: attach exactly price_bis BIS -> taker gets token, maker gets the BIS.
                       BUY : taker spends token from balance -> taker gets the BIS, maker gets the token.
  FN_CANCEL   (5)  — calldata: sel | order_id(4). Maker-only: refund the escrow (token for SELL, BIS for
                     BUY) and close the order.
  A revert commits nothing; per the VM custody model any BIS attached to a reverting call STAYS in custody,
  so attach value ONLY to FN_BUY / FN_FILL-of-a-SELL, and send EXACTLY price_bis (no change is returned).

STORAGE (32-bit word slots)
  1 = minted flag (0/1)                 2 = next order id (counter)
  0x10000000 | (party & 0x0FFFFFFF)   = token balance of a party (by CALLER fold)
  0x20000000 | (oid<<4) | field       = order record, field: 0 status(1 open,2 filled,3 cancelled)
                                         1 side(1 SELL,2 BUY) 2 token_amt 3 price_bis 4..10 maker addr(28B)

LIMITS (32-bit VM): token amounts, price (BIS units, 1 BIS = 1e8) and balances are 32-bit unsigned, so a
single price/amount tops out at ~2^32 units (~42.9 BIS); order ids < 2^24. Orders are filled WHOLE (no
partial fills) and at the maker's stated total (no price-time matching engine) — a fixed-amount limit book,
the clean overflow-free DEX for this VM. A constant-product AMM (needing the 64-bit mul/div macros) and
partial fills are the documented next step (doc/24). These mirror the other demos' demo-grade caps.
"""
import hashlib

from asmtools import (Asm, A0, A2, A3, A4, A5, A6, S0, S1, S2, T0, T1, T3, T4, T5, T6, ZERO)

# selectors
FN_INIT = 0
FN_TRANSFER = 1
FN_SELL = 2
FN_BUY = 3
FN_FILL = 4
FN_CANCEL = 5

# fixed storage slots
S_MINTED = 1
S_NEXT_OID = 2

# storage domains
TAG_BAL = 0x10000000
TAG_ORDER = 0x20000000
USER_MASK = 0x0FFFFFFF

# order record fields (within TAG_ORDER | (oid<<4) | field)
ORD_STATUS = 0
ORD_SIDE = 1
ORD_TAMT = 2
ORD_PRICE = 3
ORD_ADDR0 = 4            # 7 words (28 bytes) of the maker's payout/refund address

# enum values
ST_OPEN = 1
ST_FILLED = 2
ST_CANCELLED = 3
SIDE_SELL = 1
SIDE_BUY = 2

# scratch memory (well past code+calldata; mem is 64 KiB)
SCRATCH_ADDR = 8192     # 28-byte recipient for SYS_TRANSFER
SCRATCH_AMT = 8224      # 8-byte big-endian amount


def party_id_of(address):
    """Low 32 bits of a Bismuth address int — how the VM (truncating CALLER to 32 bits) sees a party.
    Matches vm_engine._caller_int for 56-hex (RSA) addresses."""
    try:
        return int(str(address), 16) & 0xFFFFFFFF
    except Exception:
        return int.from_bytes(hashlib.blake2b(str(address).encode(), digest_size=32).digest(),
                              "big") & 0xFFFFFFFF


def _load_be32(a, rd, base_reg, off):
    """rd = 32-bit big-endian word at [base_reg+off]."""
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


def _ord_key(a, dst, oid_reg, field):
    """dst = TAG_ORDER | (oid_reg << 4) | field. Clobbers dst, A6; oid_reg preserved."""
    a.slli(dst, oid_reg, 4)
    a.li(A6, TAG_ORDER | field)
    a.or_(dst, dst, A6)


def _credit(a, key_reg, amt_reg):
    """storage[key] += amt. Uses A4."""
    a.sload_to(A4, key_reg)
    a.add(A4, A4, amt_reg)
    a.sstore(key_reg, A4)


def _addr_to_scratch(a, oid_reg):
    """Write the 7 stored maker-address words of order `oid_reg` as 28 big-endian bytes at SCRATCH_ADDR."""
    for w in range(7):
        _ord_key(a, A3, oid_reg, ORD_ADDR0 + w)
        a.sload_to(T3, A3)                       # T3 = the stored word
        a.li(A2, SCRATCH_ADDR + w * 4)
        for i in range(4):
            a.srli(A6, T3, (3 - i) * 8)
            a.sb(A6, A2, i)


def _pay(a, amount_reg, addr_ptr_reg):
    """SYS_TRANSFER `amount_reg` units to the 28-byte address at addr_ptr_reg. Writes the amount to
    SCRATCH_AMT (via A3, scratch A6) and transfers — NOTE: addr_ptr_reg must not be A3/A6, and is read
    first by transfer() so it is never clobbered here. amount_reg must not be A3/A6 either."""
    a.li(A3, SCRATCH_AMT)
    a.store_u64_be(amount_reg, A3, 0)            # 8-byte BE amount at SCRATCH_AMT (A3)
    a.transfer(addr_ptr_reg, A3)                 # a0=addr ptr (read first), a1=amount ptr


def build(admin_id, total_supply):
    """Assemble the DEX bytecode with `admin_id` (32-bit) as the genesis minter and `total_supply` baked in."""
    if not (0 <= int(total_supply) <= 0xFFFFFFFF):
        raise ValueError("total_supply must fit a 32-bit word (<= 2^32-1 units)")
    a = Asm()

    # dispatch: S0 = calldata ptr (stable), T0 = selector, T1 = caller fold
    a.mv(S0, A0)
    _load_be32(a, T0, S0, 0)
    a.caller(T1)

    a.li(A2, FN_INIT);     a.beq(T0, A2, "init")
    a.li(A2, FN_TRANSFER); a.beq(T0, A2, "xfer")
    a.li(A2, FN_SELL);     a.beq(T0, A2, "sell")
    a.li(A2, FN_BUY);      a.beq(T0, A2, "buy")
    a.li(A2, FN_FILL);     a.beq(T0, A2, "fill")
    a.li(A2, FN_CANCEL);   a.beq(T0, A2, "cancel")
    a.j("revert")

    # --- FN_INIT: admin-only, once -> bal[admin] = total_supply --------------------------------------
    a.label("init")
    a.li(A2, admin_id); a.bne(T1, A2, "revert")
    a.li(A3, S_MINTED); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")
    _bal_key(a, T6, T1)
    a.li(A4, int(total_supply)); a.sstore(T6, A4)
    a.li(A3, S_MINTED); a.li(A4, 1); a.sstore(A3, A4)
    a.halt()

    # --- FN_TRANSFER(to28, amt): move token caller -> to ---------------------------------------------
    a.label("xfer")
    _load_be32(a, T3, S0, 32)                    # amt
    a.beq(T3, ZERO, "revert")
    _bal_key(a, T4, T1)                          # caller balance key
    a.sload_to(A4, T4); a.bltu(A4, T3, "revert"); a.sub(A4, A4, T3); a.sstore(T4, A4)   # debit caller
    _load_be32(a, T5, S0, 28)                    # recipient fold = low 32 of to(28)
    _bal_key(a, T6, T5)
    _credit(a, T6, T3)                           # credit recipient
    a.halt()

    # --- FN_SELL(maker28, token_amt, price_bis): lock token, list for BIS ----------------------------
    a.label("sell")
    _load_be32(a, T3, S0, 32)                    # token_amt
    _load_be32(a, T4, S0, 36)                    # price_bis
    a.beq(T3, ZERO, "revert"); a.beq(T4, ZERO, "revert")
    _load_be32(a, T5, S0, 28); a.bne(T5, T1, "revert")    # maker(28) must be the caller
    _bal_key(a, T6, T1)
    a.sload_to(A4, T6); a.bltu(A4, T3, "revert"); a.sub(A4, A4, T3); a.sstore(T6, A4)   # escrow token
    a.li(A2, SIDE_SELL)
    a.j("new_order")

    # --- FN_BUY(maker28, token_amt, price_bis): lock BIS (callvalue), bid for token ------------------
    a.label("buy")
    _load_be32(a, T3, S0, 32)                    # token_amt
    _load_be32(a, T4, S0, 36)                    # price_bis
    a.beq(T3, ZERO, "revert"); a.beq(T4, ZERO, "revert")
    _load_be32(a, T5, S0, 28); a.bne(T5, T1, "revert")    # maker(28) must be the caller
    a.callvalue(T5); a.bne(T5, T4, "revert")     # must attach exactly price_bis BIS (escrow in custody)
    a.li(A2, SIDE_BUY)
    # fall through to new_order

    # --- new_order: allocate oid; store status/side/amt/price + maker addr; T3=tamt T4=price A2=side -
    a.label("new_order")
    a.li(A3, S_NEXT_OID); a.sload_to(S1, A3)     # S1 = oid
    a.addi(A4, S1, 1); a.sstore(A3, A4)          # next = oid + 1
    _ord_key(a, A3, S1, ORD_STATUS); a.li(A4, ST_OPEN); a.sstore(A3, A4)
    _ord_key(a, A3, S1, ORD_SIDE);   a.sstore(A3, A2)
    _ord_key(a, A3, S1, ORD_TAMT);   a.sstore(A3, T3)
    _ord_key(a, A3, S1, ORD_PRICE);  a.sstore(A3, T4)
    for w in range(7):                           # store maker addr (calldata S0+4 .. +32) into 7 words
        _load_be32(a, T5, S0, 4 + w * 4)
        _ord_key(a, A3, S1, ORD_ADDR0 + w); a.sstore(A3, T5)
    a.mv(A0, S1); a.ret()                         # return the new order id

    # --- FN_FILL(oid, taker28): atomic settlement ----------------------------------------------------
    a.label("fill")
    _load_be32(a, S1, S0, 4)                     # S1 = oid
    _ord_key(a, A3, S1, ORD_STATUS); a.sload_to(A4, A3); a.li(A2, ST_OPEN); a.bne(A4, A2, "revert")
    _ord_key(a, A3, S1, ORD_SIDE);  a.sload_to(S2, A3)   # S2 = side
    _ord_key(a, A3, S1, ORD_TAMT);  a.sload_to(T3, A3)   # T3 = token_amt
    _ord_key(a, A3, S1, ORD_PRICE); a.sload_to(T4, A3)   # T4 = price_bis
    a.li(A2, SIDE_SELL); a.beq(S2, A2, "fill_sell")

    # FILL a BUY order: taker spends token, receives the maker's escrowed BIS; maker receives token.
    _bal_key(a, T6, T1)                          # taker (caller) token balance
    a.sload_to(A4, T6); a.bltu(A4, T3, "revert"); a.sub(A4, A4, T3); a.sstore(T6, A4)   # debit taker token
    _ord_key(a, A3, S1, ORD_ADDR0 + 6); a.sload_to(T5, A3)    # maker fold = low word of stored addr
    _bal_key(a, T6, T5); _credit(a, T6, T3)      # credit maker token
    a.addi(A4, S0, 8); _pay(a, T4, A4)           # pay taker the BIS (taker addr = calldata+8)
    a.j("fill_done")

    a.label("fill_sell")
    # FILL a SELL order: taker pays BIS (exact), receives token; maker receives the BIS.
    a.callvalue(T5); a.bne(T5, T4, "revert")     # must attach exactly price_bis
    _bal_key(a, T6, T1); _credit(a, T6, T3)      # credit taker (caller) token
    _addr_to_scratch(a, S1)                      # maker payout addr -> SCRATCH_ADDR
    a.li(A4, SCRATCH_ADDR); _pay(a, T4, A4)      # pay maker the BIS

    a.label("fill_done")
    _ord_key(a, A3, S1, ORD_STATUS); a.li(A4, ST_FILLED); a.sstore(A3, A4)
    a.halt()

    # --- FN_CANCEL(oid): maker-only, refund escrow ---------------------------------------------------
    a.label("cancel")
    _load_be32(a, S1, S0, 4)                     # S1 = oid
    _ord_key(a, A3, S1, ORD_STATUS); a.sload_to(A4, A3); a.li(A2, ST_OPEN); a.bne(A4, A2, "revert")
    _ord_key(a, A3, S1, ORD_ADDR0 + 6); a.sload_to(A4, A3); a.bne(A4, T1, "revert")   # caller == maker
    _ord_key(a, A3, S1, ORD_SIDE);  a.sload_to(S2, A3)
    _ord_key(a, A3, S1, ORD_TAMT);  a.sload_to(T3, A3)
    _ord_key(a, A3, S1, ORD_PRICE); a.sload_to(T4, A3)
    a.li(A2, SIDE_SELL); a.beq(S2, A2, "cancel_sell")
    # cancel a BUY: refund the escrowed BIS to the maker
    _addr_to_scratch(a, S1)
    a.li(A4, SCRATCH_ADDR); _pay(a, T4, A4)
    a.j("cancel_done")
    a.label("cancel_sell")
    _bal_key(a, T6, T1); _credit(a, T6, T3)      # refund token to the maker (caller == maker)
    a.label("cancel_done")
    _ord_key(a, A3, S1, ORD_STATUS); a.li(A4, ST_CANCELLED); a.sstore(A3, A4)
    a.halt()

    # --- revert: illegal instruction -> deterministic revert (no commit, no transfer) ----------------
    a.label("revert")
    a.raw(0)

    return a.assemble()


# --- read helpers (for tests / a relay reading /api/vm/contract storage) --------------------------
def balance_key(party_id):
    return TAG_BAL | (party_id & USER_MASK)


def order_key(oid, field):
    return TAG_ORDER | (oid << 4) | field
