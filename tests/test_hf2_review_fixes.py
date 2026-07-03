"""Regression tests for the post-hf2 consensus-review fixes (2026-07).

Pure/unit — no node, no network, no ledger access. Each test pins one confirmed defect closed by the
review so a future refactor can't silently reopen it. The live/consensus end-to-end behaviour is covered
by the regnet multinode suite; here we lock the mechanics.

Run with: python3 -m pytest tests/test_hf2_review_fixes.py -v
"""
from decimal import Decimal

import pytest


# --- finding 5: connections.receive length bound (single-message memory-exhaustion DoS) ----------------
def test_connections_data_len_bound():
    import connections
    connections._validate_data_len(0)                       # empty payload ok
    connections._validate_data_len(100)                     # normal ok
    connections._validate_data_len(connections.MAX_PAYLOAD)  # exactly the cap ok
    with pytest.raises(ValueError):
        connections._validate_data_len(-1)                  # negative rejected
    with pytest.raises(ValueError):
        connections._validate_data_len(connections.MAX_PAYLOAD + 1)   # oversized rejected
    with pytest.raises(ValueError):
        connections._validate_data_len(9_999_999_999)       # the 10-byte-header attack value


# --- finding 13: coinbase mining-header u8 length-prefix overflow -> clean ValueError, not OverflowError
def test_coinbase_v2_slot_overflow_is_clean_valueerror():
    import bismuth_serialize as bs
    ts, addr, recip, amt = "1700000000.00", "a" * 56, "b" * 56, "0.00000000"
    # normal coinbase: tiny nonce + ~70-byte commitment -> hashes fine and is deterministic
    good = (ts, addr, recip, amt, "deadbeef", "vmsr" + "00" * 32, "", "")
    h1 = bs.block_hash_v2([good], "00" * 28)
    h2 = bs.block_hash_v2([good], "00" * 28)
    assert h1 == h2 and len(h1) == 64                       # deterministic 32-byte blake2b hex

    # a >255-byte signature slot would overflow to_bytes(1); the guard raises a CLEAN ValueError instead
    bad_nonce = (ts, addr, recip, amt, "a" * 300, "vmsr" + "00" * 32, "", "")
    with pytest.raises(ValueError, match="u8 length prefix"):
        bs.block_hash_v2([bad_nonce], "00" * 28)
    bad_commit = (ts, addr, recip, amt, "deadbeef", "c" * 300, "", "")
    with pytest.raises(ValueError, match="u8 length prefix"):
        bs.block_hash_v2([bad_commit], "00" * 28)


# --- finding 1: dynamic-fee weight window must be deterministic (fail closed on a gap) -----------------
def test_recent_block_weights_strict_fails_closed_on_gap():
    pytest.importorskip("lmdb")
    import tempfile, os
    from block_store import BlockStore

    def _row(h, i, bh):
        return [h, "%.2f" % (1600000000 + h), "addr%d" % i, "recip%d" % i, 0.5, "sig%d" % i,
                "pub%d" % i, bh, 0.01, 1.0 if i == 0 else 0, "op", "openfield_%d_%d" % (h, i)]

    d = tempfile.mkdtemp()
    s = BlockStore(os.path.join(d, "bs"), map_size=64 * 1024 * 1024)
    try:
        for h in (4, 5, 6, 8):                               # deliberate GAP at height 7
            bh = "hash%08d" % h
            s.put_block(h, bh, [_row(h, i, bh) for i in range(2)])
        # non-strict (diagnostic): silently skips the missing height -> a SHORT window (the bug's mechanism)
        lax = s.recent_block_weights(8, window=5, strict=False)
        assert len(lax) == 4                                # 4,5,6,8 (7 skipped)
        # strict (consensus): a missing window height RAISES rather than computing a divergent average
        with pytest.raises(ValueError, match="missing height"):
            s.recent_block_weights(8, window=5, strict=True)
        # a complete window is fine under strict
        assert len(s.recent_block_weights(6, window=3, strict=True)) == 3   # 4,5,6 all present
    finally:
        s.close()


# --- finding 12: hash syscalls charge per input word (asymmetric block-validation DoS) ----------------
def test_hash_syscall_gas_is_length_proportional():
    import bismuth_riscv as rv
    from bismuth_riscv import execute, asm, addi, ecall
    A2, A7 = 12, 17

    def _keccak_gas(nbytes):
        # KECCAK256(a0=calldata ptr, a1=calldata len, a2=out) then HALT; same code, different input length.
        # out=1800 sits above the largest calldata below (32..1600 B live just past the ~20-byte code) and
        # fits the 12-bit signed addi immediate; out+32 stays inside the 64 KB memory.
        code = asm(addi(A2, 0, 1800), addi(A7, 0, rv.SYS_KECCAK256), ecall(),
                   addi(A7, 0, rv.SYS_HALT), ecall())
        r = execute(code, calldata=b"x" * nbytes)
        assert r.success
        return r.gas_used

    g1 = _keccak_gas(32)          # 1 word
    g50 = _keccak_gas(1600)       # 50 words
    # identical instruction path; the ONLY delta is the per-word hash charge -> exactly 49 words * GAS_HASH_WORD
    assert g50 - g1 == rv.GAS_HASH_WORD * (50 - 1)
    assert rv.GAS_HASH_WORD > 0   # the charge exists (flat pricing was the DoS)


# --- finding 4: fee_calculate honours the dynamic base_fee + VM surcharge (mempool/consensus parity) ---
def test_fee_calculate_dynamic_and_vm_surcharge():
    import essentials
    import fee_dynamics
    base = essentials.BASE_FEE
    # base_fee=None -> static (pre-fork / unspecified), byte-identical to the legacy call
    assert essentials.fee_calculate("", "", 0) == base
    assert essentials.fee_calculate("", "", 0, base_fee=None) == base
    # a supplied dynamic base_fee is used verbatim
    assert essentials.fee_calculate("", "", 0, base_fee=Decimal("0.05")) == Decimal("0.05000000")
    # VM surcharge applies ONLY to vm: ops and ONLY when enabled (post-fork)
    assert essentials.fee_calculate("", "vm:call", 0, vm_surcharge=True) == base + fee_dynamics.VM_SURCHARGE
    assert essentials.fee_calculate("", "vm:call", 0, vm_surcharge=False) == base   # off -> no surcharge
    assert essentials.fee_calculate("", "transfer", 0, vm_surcharge=True) == base   # non-vm op -> no surcharge
