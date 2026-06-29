"""
VM cross-chain crypto syscalls (doc/45 bridge, issue: trustless light-client bridge) — SYS_KECCAK256 and
SYS_ECRECOVER, exercised end-to-end through the REAL deterministic engine `bismuth_riscv.execute`.

These are the building blocks a Bismuth contract needs to verify ETHEREUM data (keccak-256 over RLP/MPT
nodes + secp256k1 ecrecover of signers) — the on-Bismuth half of a non-custodial bridge. They are
additive, post-fork-only with the rest of the VM (one hf2 fork gate; no second signal).

Run with: python3 -m pytest tests/test_riscv_crypto.py -v
"""
import coincurve
from Cryptodome.Hash import keccak as _keccak

import bismuth_riscv as rv
from bismuth_riscv import execute, asm, addi, lw, ecall

A0, A1, A2, A7 = 10, 11, 12, 17


def _kec(b):
    h = _keccak.new(digest_bits=256)
    h.update(b)
    return h.digest()


def test_keccak256_known_empty_vector():
    # KAT independent of any Python hash call: keccak256("") = c5d2460186f7233c...85a470.
    code = asm(addi(A2, 0, 2000), addi(A7, 0, rv.SYS_KECCAK256), ecall(),
               lw(A0, A2, 0), addi(A7, 0, rv.SYS_RETURN), ecall())
    r = execute(code, calldata=b"")
    expected = int.from_bytes(bytes.fromhex("c5d24601"), "little")   # first word, lw is little-endian
    assert r.success and int.from_bytes(r.output, "big") == expected


def test_keccak256_via_ecall():
    # KECCAK256(a0=ptr, a1=len, a2=out): hash the calldata, return its first word.
    data = b"bismuth-bridge-keccak-vector!!!1"
    code = asm(addi(A2, 0, 2000), addi(A7, 0, rv.SYS_KECCAK256), ecall(),
               lw(A0, A2, 0), addi(A7, 0, rv.SYS_RETURN), ecall())
    r = execute(code, calldata=data)
    expected = int.from_bytes(_kec(data)[0:4], "little")
    assert r.success and int.from_bytes(r.output, "big") == expected
    # and it is genuinely keccak, NOT sha256 (different digest for the same input)
    import hashlib
    assert _kec(data)[0:4] != hashlib.sha256(data).digest()[0:4]


def test_ecrecover_recovers_eth_address():
    # ECRECOVER(a0=hash ptr(32), a1=sig ptr(65), a2=out ptr(20)) -> a0=1 + 20-byte address at out.
    # calldata = msg_hash(32) | sig(65); a1 = a0+32 (sig), a2 = 2000 (out); return out's first word.
    pk = coincurve.PrivateKey(secret=b"\x11" * 32)
    msg_hash = _kec(b"bridge-auth-message")
    sig = pk.sign_recoverable(msg_hash, hasher=None)             # 65-byte r|s|recid(0/1)
    exp_addr = _kec(pk.public_key.format(compressed=False)[1:])[-20:]
    code = asm(addi(A1, A0, 32), addi(A2, 0, 2000),
               addi(A7, 0, rv.SYS_ECRECOVER), ecall(),
               lw(A0, A2, 0), addi(A7, 0, rv.SYS_RETURN), ecall())
    r = execute(code, calldata=msg_hash + sig)
    expected = int.from_bytes(exp_addr[0:4], "little")
    assert r.success and int.from_bytes(r.output, "big") == expected


def test_ecrecover_accepts_eth_v27_28():
    # An Ethereum-style sig carries v = 27/28; the syscall must accept that as well as raw 0/1.
    pk = coincurve.PrivateKey(secret=b"\x22" * 32)
    msg_hash = _kec(b"eth-style-v")
    sig = bytearray(pk.sign_recoverable(msg_hash, hasher=None))
    sig[64] = 27 + sig[64]                                        # 0/1 -> 27/28
    exp_addr = _kec(pk.public_key.format(compressed=False)[1:])[-20:]
    code = asm(addi(A1, A0, 32), addi(A2, 0, 2000),
               addi(A7, 0, rv.SYS_ECRECOVER), ecall(),
               lw(A0, A2, 0), addi(A7, 0, rv.SYS_RETURN), ecall())
    r = execute(code, calldata=msg_hash + bytes(sig))
    assert r.success and int.from_bytes(r.output, "big") == int.from_bytes(exp_addr[0:4], "little")


def test_ecrecover_rejects_bad_sig():
    # A malformed signature must be a clean a0=0 (NOT a leaked exception / non-deterministic fault).
    code = asm(addi(A1, A0, 32), addi(A2, 0, 2000),
               addi(A7, 0, rv.SYS_ECRECOVER), ecall(),
               addi(A7, 0, rv.SYS_RETURN), ecall())              # return the success flag
    bad = b"\x00" * 32 + b"\x00" * 65                            # zero hash + zero (invalid) sig
    r = execute(code, calldata=bad)
    assert r.success and int.from_bytes(r.output, "big") == 0


def test_crypto_syscalls_are_deterministic():
    pk = coincurve.PrivateKey(secret=b"\x33" * 32)
    msg_hash = _kec(b"determinism")
    sig = pk.sign_recoverable(msg_hash, hasher=None)
    code = asm(addi(A1, A0, 32), addi(A2, 0, 2000),
               addi(A7, 0, rv.SYS_ECRECOVER), ecall(),
               lw(A0, A2, 0), addi(A7, 0, rv.SYS_RETURN), ecall())
    a = execute(code, calldata=msg_hash + sig)
    b = execute(code, calldata=msg_hash + sig)
    assert a.success and a.output == b.output and a.gas_used == b.gas_used
