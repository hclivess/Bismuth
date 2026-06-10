"""
raffle.py — a commit-reveal RAFFLE / lottery as one hand-authored RV32I contract for the Bismuth
decentralized-apps VM (bismuth_riscv / vm_engine). Demonstrates VALUE CUSTODY + a deterministic,
verifiable draw built on SYS_SHA256 (no host randomness — the VM has none, by design).

WHAT IT DOES
  Anyone ENTERs by sending BIS; each entry is a ticket recorded in order. The admin DRAWs by revealing a
  seed whose hash was committed at deploy; the contract verifies SHA256(seed) == committed hash and then
  picks the winning ticket deterministically from that (now-revealed) hash. The single winner CLAIMs the
  whole pot to an address they supply, proving they are the winner via SYS_CALLER. One terminal draw,
  one claim.

WHY COMMIT-REVEAL (and its honest limit)
  The VM is a pure function of (code, calldata, context, storage) — there is NO randomness syscall and
  block height is miner-grindable, so a fair draw needs entropy that is fixed yet unknown-until-revealed.
  The admin commits H = SHA256(seed) at DEPLOY (before entries open) and reveals `seed` at DRAW; the
  contract recomputes the hash and rejects any other preimage, so the admin cannot change the seed after
  seeing the entrants. DEMO-GRADE FAIRNESS CAVEAT: because the winning *index* is a function of the
  committed hash, the admin knows the winning index in advance (not who will occupy it). A production
  raffle should fold in entropy unpredictable to the admin AND fixed after entries close (a future block
  hash / VRF); that is deliberately out of scope for this demo, which exists to show SYS_SHA256 + custody.

DEPLOY
  operation = "vm:deploy", openfield = build(admin_id, seed_hash).hex()
    admin_id  = party_id_of(admin_address)  — low 32 bits of the admin's Bismuth address (CALLER fold).
    seed_hash = 32 bytes = SHA256(seed) for a secret 32-byte `seed` the admin keeps until the draw.
  Both are baked into the bytecode.

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>")
  selector = first 4 bytes big-endian:
    FN_ENTER (0) — anyone; send BIS to VM_SINK with this call to buy a ticket (accumulates into the pot;
                   each call is one ticket for the caller). Reverts once drawn or if no value attached.
    FN_DRAW  (1) — admin-only; calldata = selector(4) | seed(32 BE). Verifies SHA256(seed) == committed
                   hash, then sets the winning ticket = hash_word0 mod num_tickets. Reverts otherwise.
    FN_CLAIM (2) — winner-only; calldata = selector(4) | recipient(28 BE). Pays the whole pot to the
                   recipient. Claim-once. Caller must be the winning ticket's entrant.
  A revert commits nothing and transfers nothing.

STORAGE (32-bit word slots)
  1 = drawn flag (0 open / 1 drawn)      2 = ticket count (next index)     3 = pot held (units)
  4 = winner fold (set at draw)          5 = claimed flag (0 / 1)
  0x10000000 | i = ticket i's entrant CALLER fold (i in 0..count-1)
"""
import hashlib

from asmtools import (Asm, A0, A1, A2, A3, A4, A5, S0, T0, T1, T2, T3, T4, T5, T6, ZERO,
                      SYS_SHA256)

FN_ENTER = 0
FN_DRAW = 1
FN_CLAIM = 2

S_DRAWN = 1
S_NUM = 2
S_POT = 3
S_WINNER = 4
S_CLAIMED = 5
TAG_TICKET = 0x10000000        # slot (TAG_TICKET | index) -> that ticket's entrant fold
SCRATCH = 4096                 # mem scratch for the SHA256 output and the 8-byte transfer amount


def party_id_of(address):
    """Low 32 bits of a Bismuth address int — the VM's 32-bit fold of CALLER (same as the other demos)."""
    try:
        return int(str(address), 16) & 0xFFFFFFFF
    except Exception:
        return int.from_bytes(hashlib.blake2b(str(address).encode(), digest_size=32).digest(),
                              "big") & 0xFFFFFFFF


def seed_hash_of(seed: bytes) -> bytes:
    """The 32-byte commitment baked at deploy. `seed` is a secret 32-byte value revealed at draw."""
    return hashlib.sha256(seed).digest()


def winning_index(seed: bytes, num_tickets: int) -> int:
    """Off-chain mirror of the on-chain selection: hash_word0 mod num_tickets. Lets a UI/test predict
    (and verify) the winner the contract will choose for a given revealed seed."""
    word0 = int.from_bytes(hashlib.sha256(seed).digest()[0:4], "big")
    return word0 % num_tickets


def _load_be32(a, rd, base_reg, off):
    """rd = big-endian uint32 at mem[base_reg+off .. +off+3]. Clobbers A2."""
    a.lbu(rd, base_reg, off)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 1); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 2); a.or_(rd, rd, A2)
    a.slli(rd, rd, 8); a.lbu(A2, base_reg, off + 3); a.or_(rd, rd, A2)


def build(admin_id, seed_hash):
    """Assemble the raffle with the admin id and the 32-byte committed seed hash baked in."""
    if len(seed_hash) != 32:
        raise ValueError("seed_hash must be 32 bytes (SHA256 of the secret seed)")
    words = [int.from_bytes(seed_hash[4 * i:4 * i + 4], "big") for i in range(8)]   # big-endian words

    a = Asm()
    a.mv(S0, A0)                                   # stash calldata ptr (syscalls clobber a0)
    _load_be32(a, T0, S0, 0)                        # T0 = selector
    a.caller(T1)                                   # T1 = caller fold (survives later syscalls)

    a.li(A2, FN_ENTER); a.beq(T0, A2, "enter")
    a.li(A2, FN_DRAW);  a.beq(T0, A2, "draw")
    a.li(A2, FN_CLAIM); a.beq(T0, A2, "claim")
    a.j("revert")

    # --- ENTER: anyone, while open; record a ticket + accumulate the pot ----------------------------
    a.label("enter")
    a.li(A3, S_DRAWN); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")     # not after the draw
    a.callvalue(T3); a.beq(T3, ZERO, "revert")                          # must attach value
    a.li(A3, S_NUM); a.sload_to(T2, A3)                                 # T2 = count = this ticket's index
    a.li(A4, TAG_TICKET); a.add(A4, A4, T2)                            # slot = TAG_TICKET + index
    a.sstore(A4, T1)                                                    # ticket[index] = caller fold
    a.li(A3, S_NUM); a.addi(T2, T2, 1); a.sstore(A3, T2)               # count += 1
    a.li(A3, S_POT); a.sload_to(A4, A3); a.add(A4, A4, T3); a.sstore(A3, A4)  # pot += callvalue
    a.halt()

    # --- DRAW: admin-only; verify revealed seed, pick the winning ticket ----------------------------
    a.label("draw")
    a.li(A2, admin_id); a.bne(T1, A2, "revert")                        # admin-only
    a.li(A3, S_DRAWN); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")    # not already drawn
    a.li(A3, S_NUM); a.sload_to(T2, A3); a.beq(T2, ZERO, "revert")      # need >= 1 ticket (T2 = N)
    # SHA256(seed) -> SCRATCH ; seed is calldata+4, length 32
    a.addi(A0, S0, 4); a.li(A1, 32); a.li(A2, SCRATCH); a.syscall(SYS_SHA256)
    # verify the 32-byte hash equals the committed seed hash (all 8 words — a partial check would let the
    # admin grind a different preimage matching only the checked bytes)
    a.li(A3, SCRATCH)
    for i in range(8):
        _load_be32(a, T0, A3, 4 * i)                                   # T0 = hash word i (clobbers A2)
        a.li(A2, words[i]); a.bne(T0, A2, "revert")
    # winner index = hash_word0 mod N ; reload word0 (verify clobbered T0 each iter)
    a.li(A3, SCRATCH); _load_be32(a, T0, A3, 0)                        # T0 = hash word0
    a.divu(T4, T3, T0, T2, T5, T6)                                     # T3 = word0 % N  (T4 = quotient)
    a.li(A4, TAG_TICKET); a.add(A4, A4, T3); a.sload_to(A5, A4)        # A5 = winning ticket's entrant fold
    a.li(A3, S_WINNER); a.sstore(A3, A5)                              # record the winner
    a.li(A3, S_DRAWN); a.li(A4, 1); a.sstore(A3, A4)                  # drawn = 1
    a.halt()

    # --- CLAIM: winner-only, claim-once; pay the whole pot to the supplied recipient -----------------
    a.label("claim")
    a.li(A3, S_DRAWN); a.sload_to(A4, A3); a.beq(A4, ZERO, "revert")    # must be drawn
    a.li(A3, S_CLAIMED); a.sload_to(A4, A3); a.bne(A4, ZERO, "revert")  # not already claimed
    a.li(A3, S_WINNER); a.sload_to(A4, A3); a.bne(T1, A4, "revert")     # caller must be the winner
    a.li(A3, S_CLAIMED); a.li(A4, 1); a.sstore(A3, A4)                 # claim BEFORE transfer (fail-safe)
    a.li(A3, S_POT); a.sload_to(T3, A3); a.beq(T3, ZERO, "revert")      # nothing to pay
    a.li(A4, SCRATCH); a.store_u64_be(T3, A4, 0)                        # 8-byte BE amount = pot
    a.addi(A3, S0, 4)                                                  # recipient ptr (calldata+4, 28 B)
    a.transfer(A3, A4)
    a.halt()

    a.label("revert")
    a.raw(0)                                                           # illegal instruction -> revert
    return a.assemble()
