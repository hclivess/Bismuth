"""hf2: operation/openfield are free-form and UNCAPPED post-fork (doc/41, generalized from the coinbase
to every tx), while the legacy 30 / 100000 truncation stays byte-identical PRE-fork (frozen consensus).

These lock the invariant the adversarial review surfaced: dropping the digest_tx truncation unconditionally
split upgraded vs legacy nodes on the still-live pre-fork chain, because a peer-relayed block bypasses the
mempool caps and the digest_tx value feeds the pre-fork signing buffer + legacy block hash.
"""
import digest_tx
import txrec
import sync_codec


def _raw(op, of):
    # non-coinbase raw wire tuple [ts, addr, recip, amount, sig, pubkey, operation, openfield]
    return [1700000000.0, "addr", "recip", "1.00000000", "sig", "pubkey", op, of]


def _parse(op, of, block_height, fork_height):
    tx = digest_tx.Transaction()
    # index 0 of 2 -> NOT the last tx, so it is treated as an ordinary (non-coinbase) transaction
    tx.from_raw_transaction(_raw(op, of), 0, 2, block_height=block_height, fork_height=fork_height)
    return tx


def test_prefork_truncates_when_fork_unset():
    # fork_height None == pre-fork / not locked in (mainnet today): legacy 30 / 100000 truncation preserved.
    tx = _parse("x" * 31, "y" * 100001, block_height=None, fork_height=None)
    assert tx.received_operation == "x" * 30
    assert tx.received_openfield == "y" * 100000


def test_prefork_truncates_below_fork_height():
    tx = _parse("x" * 31, "y" * 100001, block_height=100, fork_height=200)
    assert tx.received_operation == "x" * 30
    assert tx.received_openfield == "y" * 100000


def test_postfork_keeps_full_value_at_and_after_fork():
    # boundary: height == fork_height is post-fork (inclusive), like every other hf2 gate.
    at = _parse("x" * 31, "y" * 100001, block_height=200, fork_height=200)
    assert at.received_operation == "x" * 31
    assert at.received_openfield == "y" * 100001
    after = _parse("o" * 5000, "d" * 500000, block_height=300, fork_height=200)
    assert after.received_operation == "o" * 5000
    assert after.received_openfield == "d" * 500000


def test_txrec_roundtrip_operation_over_255_bytes():
    # operation prefix widened u8 -> u32; previously > 255 bytes raised in _lp1.
    row = [1700000000.0, 5, 6, "2.50000000", b"\x01\x00\x02ab", 3, None,
           "0.01000000", "0.00000000", "x" * 5000, "k" * 200000]
    out = txrec.unpack_row(txrec.pack_row(row, 5, 6))
    assert out[9] == "x" * 5000
    assert out[10] == "k" * 200000


def test_sync_codec_roundtrip_operation_over_255_bytes():
    # transport prefix widened u8 -> u32; previously > 255 bytes OverflowError'd in _lp1.
    blk = {"block_height": 4900001, "block_hash": "ab" * 28,
           "transactions": [[1700000000.0, "Bis1a", "Bis1b", "2.50000000", "sig", "pub",
                             "vm:call" + "X" * 5000, "D" * 200000]]}
    back = sync_codec.decode_blocks(sync_codec.encode_blocks([blk]))
    tx = back[0]["transactions"][0]
    assert tx[6] == "vm:call" + "X" * 5000
    assert tx[7] == "D" * 200000
