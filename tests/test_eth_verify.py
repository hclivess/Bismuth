"""
contracts/eth_verify.py — the Ethereum-signer authenticator (doc/45 bridge), driven end-to-end through the
REAL deterministic engine. Proves SYS_KECCAK256 + SYS_ECRECOVER compose into on-chain Ethereum
signature verification: the contract keccak's the payload, ecrecovers the signer, and authorises iff the
signer is the baked Ethereum address.

Run with: python3 -m pytest tests/test_eth_verify.py -v
"""
import os
import sys

import coincurve
from Cryptodome.Hash import keccak as _keccak

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "contracts"))

import bismuth_riscv
import eth_verify


def _kec(b):
    h = _keccak.new(digest_bits=256)
    h.update(b)
    return h.digest()


def _eth_addr(pk):
    return _kec(pk.public_key.format(compressed=False)[1:])[-20:]


def _run(code, payload, sig):
    return bismuth_riscv.execute(code, calldata=payload + sig)


def test_authorised_signer_is_accepted():
    pk = coincurve.PrivateKey(secret=b"\x44" * 32)
    code = eth_verify.build(_eth_addr(pk))
    payload = _kec(b"burn-commitment-#1")              # any 32-byte payload
    sig = pk.sign_recoverable(_kec(payload), hasher=None)
    r = _run(code, payload, sig)
    assert r.success and int.from_bytes(r.output, "big") == 1


def test_wrong_signer_is_rejected():
    authorised = coincurve.PrivateKey(secret=b"\x44" * 32)
    attacker = coincurve.PrivateKey(secret=b"\x99" * 32)
    code = eth_verify.build(_eth_addr(authorised))
    payload = _kec(b"burn-commitment-#2")
    sig = attacker.sign_recoverable(_kec(payload), hasher=None)   # signed by the WRONG key
    r = _run(code, payload, sig)
    assert r.success and int.from_bytes(r.output, "big") == 0


def test_tampered_payload_is_rejected():
    # A valid signature over a DIFFERENT payload must not authorise this one (the keccak'd hash differs,
    # so ecrecover yields a different address than the signer's).
    pk = coincurve.PrivateKey(secret=b"\x44" * 32)
    code = eth_verify.build(_eth_addr(pk))
    sig = pk.sign_recoverable(_kec(_kec(b"original")), hasher=None)
    r = _run(code, _kec(b"tampered"), sig)
    assert r.success and int.from_bytes(r.output, "big") == 0


def test_garbage_signature_is_rejected():
    pk = coincurve.PrivateKey(secret=b"\x44" * 32)
    code = eth_verify.build(_eth_addr(pk))
    r = _run(code, _kec(b"x"), b"\x00" * 65)
    assert r.success and int.from_bytes(r.output, "big") == 0


def test_high_bit_address_bytes_compare_correctly():
    # Exercise byte values >= 0x80 in the baked address compare (guards the addi/li immediate path).
    for seed in (b"\x07" * 32, b"\xab" * 32, b"\xfe" * 32):
        pk = coincurve.PrivateKey(secret=seed)
        code = eth_verify.build(_eth_addr(pk))
        payload = _kec(b"hi-bit-" + seed[:1])
        sig = pk.sign_recoverable(_kec(payload), hasher=None)
        r = _run(code, payload, sig)
        assert r.success and int.from_bytes(r.output, "big") == 1, "seed %r should authorise" % seed[:1]
