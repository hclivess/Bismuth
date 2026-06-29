"""
eth_verify.py — an Ethereum-signer AUTHENTICATOR as one hand-authored RV32I contract for the Bismuth VM
(doc/45 bridge). It is the building block of the peg-OUT verifier: "was this action authorised by the
Ethereum address the bridge trusts?" It composes BOTH new bridge syscalls end-to-end:

    keccak256(payload) -> hash   (SYS_KECCAK256, the hash Ethereum signs over)
    ecrecover(hash, sig) -> 20-byte signer address   (SYS_ECRECOVER)
    signer == the baked authorised address ?  -> 1 (authorised) / 0 (not)

This proves the Stage-1 crypto syscalls actually COMPOSE into real Ethereum verification, on-chain and
deterministically. A full peg-out verifier additionally checks the payload is a burn log committed in a
finalised Ethereum block (RLP + Merkle-Patricia proof under the header, + sync-committee finality) — that
builds ON this primitive; here we isolate + prove the signer-authentication core.

DEPLOY
  operation = "vm:deploy", openfield = build(authorised_eth_addr_20_bytes).hex()
    the 20-byte authorised Ethereum address is baked into the bytecode.

CALL ABI  (operation = "vm:call", openfield = "<contract_addr>:<calldata_hex>")
  calldata = payload(32) | sig(65 = r|s|v)
    payload : the 32-byte message the Ethereum key signed over (e.g. a burn-event commitment / header root)
    sig     : the 65-byte recoverable secp256k1 signature
  returns (SYS_RETURN word) = 1 iff ecrecover(keccak256(payload), sig) == the baked authorised address,
                              else 0 (also 0 on an unrecoverable/garbage signature).

Hand-authored RV32I via asmtools (same idiom as escrow.py / ownable.py). The address compare is byte-wise
(each baked byte is 0..255, well within addi's range — no lui carry to get wrong).
"""
from asmtools import Asm, A0, A3, S0, T0, T1, T3, ZERO

HASH = 4096    # scratch: keccak256(payload) output (32 bytes)
OUT = 4128     # scratch: recovered 20-byte ETH address


def build(eth_addr):
    """Assemble the authenticator with `eth_addr` (20 raw bytes) baked in as the authorised signer."""
    if len(eth_addr) != 20:
        raise ValueError("eth_addr must be 20 bytes (a raw Ethereum address)")
    a = Asm()
    a.mv(S0, A0)                       # calldata ptr: payload(32) @ +0, sig(65) @ +32
    a.li(A3, 32)                       # payload length
    a.li(T1, HASH)                     # keccak output ptr
    a.keccak256(S0, A3, T1)            # [HASH] = keccak256(payload)
    a.addi(T0, S0, 32)                 # sig ptr = calldata + 32
    a.li(T3, OUT)                      # recovered-address out ptr
    a.ecrecover(T1, T0, T3)            # a0 = 1/0 ; [OUT] = recovered 20-byte address
    a.beq(A0, ZERO, "fail")            # signature did not recover -> not authorised
    for i in range(20):                # recovered address == baked authorised address ? (byte-wise)
        a.lbu(T0, T3, i)
        a.li(A3, eth_addr[i])
        a.bne(T0, A3, "fail")
    a.li(A0, 1)
    a.ret()                            # authorised signer
    a.label("fail")
    a.li(A0, 0)
    a.ret()                            # not authorised / unrecoverable
    return a.assemble()
