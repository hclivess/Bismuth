"""
multisig.py — an M-of-N MULTISIG vault as one hand-authored RV32I contract for the Bismuth
decentralized-apps VM. The postfork analogue of Bitcoin's script-based multisig (P2SH): funds are
custodied by the contract and released only when at least M of the N owners approve a payout. Requires
NO node/consensus change — it rides the existing hf2 VM, exactly like the escrow/raffle demos.

WHY A CONTRACT (not a native multisig signer): the node verifies ONE signature per tx against the
sender's address; true on-chain M-of-N signature aggregation would change the consensus signature path
(its own hard fork — noted as deferred in doc/23). A custody contract delivers M-of-N today, on the VM
layer, fully testable.

MODEL — one standing proposal at a time:
  DEPOSIT  — anyone funds the vault (callvalue accumulates the pot).
  PROPOSE  — an OWNER proposes a payout (recipient, amount): stores it, clears all approvals, and counts
             as the proposer's own approval. A new PROPOSE supersedes any prior one (approvals reset).
  APPROVE  — an OWNER approves the standing proposal (idempotent; one owner = one approval).
  EXECUTE  — ANYONE may execute once >= M distinct owners have approved and the pot covers the amount;
             pays the stored recipient and marks it executed (so it can't pay twice).

DEPLOY
  operation = "vm:deploy", openfield = build([owner_id, ...], threshold).hex()
    owner_id = party_id_of(owner_address)  (low 32 bits of the address — the VM's CALLER fold).
    1 <= threshold <= N <= 15.

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>")
  FN_DEPOSIT (0) — send BIS to VM_SINK with this call; pot += callvalue.
  FN_PROPOSE (1) — owner-only; calldata = selector(4) | recipient(28 BE) | amount(4 BE, units).
  FN_APPROVE (2) — owner-only; selector only; approves the standing proposal.
  FN_EXECUTE (3) — anyone; selector only; pays the stored proposal if approvals >= threshold.
  A revert commits nothing and transfers nothing.

STORAGE (32-bit word slots)
  1 = pot (units)   2 = proposed amount (units)   3 = proposed flag   4 = executed flag
  10..16 = recipient as 7 big-endian words        0x20000000 | i = owner i approved the standing proposal

NOTE (demo amount cap): the proposed amount is a single 32-bit word (like escrow's stored amount), so a
single payout is bounded by ~42.9 BIS in integer units — fine for regnet/demo. A production vault would
carry a 64-bit amount; out of scope here.
"""
import hashlib

from asmtools import Asm, A0, A2, A3, A4, A5, A6, S0, S1, T0, T1, T2, ZERO

FN_DEPOSIT = 0
FN_PROPOSE = 1
FN_APPROVE = 2
FN_EXECUTE = 3

S_POT = 1
S_AMT = 2
S_PROPOSED = 3
S_EXECUTED = 4
S_R0 = 10                       # recipient words 10..16 (7 words = 28 bytes, big-endian)
TAG_APPROVE = 0x20000000        # slot (TAG_APPROVE | owner_index) = approved-current-proposal flag
SCRATCH = 4096                  # mem: recipient at +0 (28 B), amount at +32 (8 B BE)

MAX_OWNERS = 15


def party_id_of(address):
    """Low 32 bits of a Bismuth address int — the VM's 32-bit fold of CALLER."""
    try:
        return int(str(address), 16) & 0xFFFFFFFF
    except Exception:
        return int.from_bytes(hashlib.blake2b(str(address).encode(), digest_size=32).digest(),
                              "big") & 0xFFFFFFFF


def _load_be32(a, rd, base_reg, off):
    """rd = big-endian uint32 at mem[base_reg+off .. +3]. Clobbers A2."""
    a.lbu(rd, base_reg, off)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 1); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 2); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 3); a.or_(rd, rd, A2)


def _store_be32(a, val_reg, base_reg, off):
    """Write val_reg big-endian (MSB first) to mem[base_reg+off .. +3]. Clobbers A6."""
    for i in range(4):
        a.srli(A6, val_reg, (3 - i) * 8)
        a.sb(A6, base_reg, off + i)


def _set_caller_approval(a, owner_ids, tag):
    """Emit: find the caller (T1) among the baked owners and set its approval bit; revert if not an owner.
    `tag` makes the generated labels unique across the two call sites (propose/approve)."""
    for i, oid in enumerate(owner_ids):
        a.li(A2, oid); a.bne(T1, A2, "ms_no_%s_%d" % (tag, i))
        a.li(A3, TAG_APPROVE | i); a.li(A4, 1); a.sstore(A3, A4)
        a.j("ms_set_done_%s" % tag)
        a.label("ms_no_%s_%d" % (tag, i))
    a.j("revert")                                   # caller matched no owner
    a.label("ms_set_done_%s" % tag)


def build(owner_ids, threshold):
    """Assemble an M-of-N vault with the owner folds + threshold baked in."""
    n = len(owner_ids)
    if not (1 <= threshold <= n <= MAX_OWNERS):
        raise ValueError("need 1 <= threshold <= N <= %d" % MAX_OWNERS)
    if len(set(owner_ids)) != n:
        raise ValueError("owner ids must be distinct")

    a = Asm()
    a.mv(S0, A0)                                    # stash calldata ptr
    _load_be32(a, T0, S0, 0)                         # T0 = selector
    a.caller(T1)                                    # T1 = caller fold

    a.li(A2, FN_DEPOSIT); a.beq(T0, A2, "deposit")
    a.li(A2, FN_PROPOSE); a.beq(T0, A2, "propose")
    a.li(A2, FN_APPROVE); a.beq(T0, A2, "approve")
    a.li(A2, FN_EXECUTE); a.beq(T0, A2, "execute")
    a.j("revert")

    # --- DEPOSIT: anyone; pot += callvalue ----------------------------------------------------------
    a.label("deposit")
    a.callvalue(T2); a.beq(T2, ZERO, "revert")
    a.li(A3, S_POT); a.sload_to(A4, A3); a.add(A4, A4, T2); a.sstore(A3, A4)
    a.halt()

    # --- PROPOSE: owner-only; store payout, reset approvals, approve as proposer --------------------
    a.label("propose")
    # store recipient (7 words, calldata+4..+31) and amount (calldata+32). A revert below rolls these back.
    for k in range(7):
        _load_be32(a, A5, S0, 4 + 4 * k); a.li(A3, S_R0 + k); a.sstore(A3, A5)
    _load_be32(a, A5, S0, 32); a.li(A3, S_AMT); a.sstore(A3, A5)
    a.li(A3, S_PROPOSED); a.li(A4, 1); a.sstore(A3, A4)
    a.li(A3, S_EXECUTED); a.sstore(A3, ZERO)
    for i in range(n):                               # clear every owner's approval bit
        a.li(A3, TAG_APPROVE | i); a.sstore(A3, ZERO)
    _set_caller_approval(a, owner_ids, "prop")       # proposer auto-approves (reverts if not an owner)
    a.halt()

    # --- APPROVE: owner-only; approve the standing proposal -----------------------------------------
    a.label("approve")
    a.li(A3, S_PROPOSED); a.sload_to(A4, A3); a.beq(A4, ZERO, "revert")   # nothing proposed
    _set_caller_approval(a, owner_ids, "appr")
    a.halt()

    # --- EXECUTE: anyone; pay if approvals >= threshold ---------------------------------------------
    a.label("execute")
    a.li(A3, S_PROPOSED); a.sload_to(A4, A3); a.beq(A4, ZERO, "revert")   # nothing proposed
    a.li(A3, S_EXECUTED); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")   # already executed
    # count approvals
    a.addi(T2, ZERO, 0)
    for i in range(n):
        a.li(A3, TAG_APPROVE | i); a.sload_to(A4, A3); a.add(T2, T2, A4)
    a.li(A2, threshold); a.blt(T2, A2, "revert")                          # need >= threshold approvals
    # amount must be covered by the pot
    a.li(A3, S_AMT); a.sload_to(T0, A3)                                   # T0 = amount
    a.li(A3, S_POT); a.sload_to(T1, A3)                                   # T1 = pot
    a.bltu(T1, T0, "revert")                                              # pot < amount -> revert
    # mark executed BEFORE transfer (fail-safe), decrement pot
    a.li(A3, S_EXECUTED); a.li(A4, 1); a.sstore(A3, A4)
    a.sub(A4, T1, T0); a.li(A3, S_POT); a.sstore(A3, A4)                  # pot -= amount
    # reconstruct recipient (28 bytes) + amount (8-byte BE) in scratch, then transfer
    a.li(S1, SCRATCH)
    for k in range(7):
        a.li(A3, S_R0 + k); a.sload_to(A5, A3); _store_be32(a, A5, S1, 4 * k)
    a.store_u64_be(T0, S1, 32)                                            # amount BE at SCRATCH+32
    a.addi(A4, S1, 32)                                                    # amount ptr
    a.transfer(S1, A4)                                                    # SYS_TRANSFER(recipient, amount)
    a.halt()

    a.label("revert")
    a.raw(0)                                                              # illegal instruction -> revert
    return a.assemble()
