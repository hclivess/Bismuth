"""
LMDB block-body store tests (block_store.BlockStore) — pure unit, no node/network.

Proves: lossless 12-field round-trip (get_block_raw) + typed reads (get_block), NUMERIC height ordering
(big-endian keys), hash->height index, height-based rollback (including its hash-index cleanup), the
pubkey dedup, the on-disk byte layout, and the post-fork true-bytes body codec.

Run with: python3 -m pytest tests/test_block_store.py -v
"""
import pytest

import block_store
from block_store import BlockStore

SMALL = 64 * 1024 * 1024  # 64 MiB map is plenty for tests


def _row(h, i, bh):
    # a 12-field ledger row: block_height, timestamp, address, recipient, amount, signature,
    # public_key, block_hash, fee, reward, operation, openfield
    return [h, "%.2f" % (1600000000 + h), "addr%d" % i, "recip%d" % i, 0.5 + i,
            "sig_%d_%d" % (h, i), "pubkey%d" % i, bh, 0.01, 1.0 if i == 0 else 0,
            "op%d" % i, "openfield_%d_%d" % (h, i)]


def _block(h, ntx=2):
    bh = "hash%08d" % h
    return h, bh, [_row(h, i, bh) for i in range(ntx)]


def _commit(s, blocks, fork_height=None):
    for h, bh, rows in blocks:
        s.commit_block(h, bh, rows, fork_height=fork_height)


def test_put_get_lossless_roundtrip(tmp_path):
    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        h, bh, rows = _block(5, ntx=3)
        s.commit_block(h, bh, rows)
        assert s.get_block_raw(5) == rows     # exact 12-field round-trip (as written)
        assert s.get_block(5) == [block_store.typed_row(r) for r in rows]   # typed read
        assert s.block_hash(5) == bh
        assert s.height_by_hash(bh) == 5
        assert s.get_block(99) is None
        assert s.height_by_hash("nope") is None
    finally:
        s.close()


def test_numeric_ordering_tip_count_range(tmp_path):
    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        _commit(s, [_block(h) for h in range(1, 12)])
        assert s.tip() == 11
        assert s.count() == 11
        # ordering must stay NUMERIC (would be 10<2 if keys were decimal strings)
        assert [h for h, _ in s.blocks_in_range(2, 10)] == list(range(2, 11))
        assert [h for h, _ in s.blocks_in_range(9, 100)] == [9, 10, 11]
        assert [h for h, _ in s.blocks_desc(10)][:3] == [10, 9, 8]
        with pytest.raises(ValueError):          # commits must extend the tip
            s.commit_block(100, "hash%08d" % 100, _block(100)[2])
    finally:
        s.close()


def test_rollback_removes_blocks_and_hashes(tmp_path):
    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        _commit(s, [_block(h) for h in range(1, 11)])
        assert s.tip() == 10 and s.count() == 10
        removed = s.rollback(6)                   # 7..10 removed, rows handed back ascending
        assert [r[0] for r in removed] == [7, 7, 8, 8, 9, 9, 10, 10]
        assert s.tip() == 6 and s.count() == 6
        assert s.get_block(7) is None
        assert s.get_block(6) is not None
        assert s.height_by_hash("hash%08d" % 7) is None   # hash index cleaned up
        assert s.height_by_hash("hash%08d" % 6) == 6
    finally:
        s.close()


def test_pubkey_dedup(tmp_path):
    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        # 3 blocks * 2 txs each = 6 txs, but only 2 distinct public keys (pubkey0 / pubkey1 from _row)
        _commit(s, [_block(h, ntx=2) for h in (1, 2, 3)])
        with s.env.begin() as txn:
            assert txn.stat(db=s.pk)["entries"] == 2     # deduped: 2 keys stored, not 6
        for h in (1, 2, 3):                              # and still lossless
            _, _, rows = _block(h, ntx=2)
            assert s.get_block_raw(h) == rows
    finally:
        s.close()


def test_long_pubkey_roundtrip(tmp_path):
    # a real RSA public key is ~1068 bytes, bigger than LMDB's 511-byte max key size; the dedup must
    # hash it for the table key, not use it directly.
    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        longpk = "A" * 1100
        rows = [[7, "t", "addr", "recip", 5, "sig", longpk, "hashlong", 0, 0, "", ""]]
        s.commit_block(7, "hashlong", rows)
        assert s.get_block_raw(7) == rows
    finally:
        s.close()


def test_on_disk_bytes_identical_to_direct_lmdb(tmp_path):
    """The BlockStore's on-disk layout: big-endian height keys, the msgpack {"h","t"} block value, raw
    hash->height, and the blake2b-keyed pubkey dedup tables. Read the sub-dbs back with a RAW direct lmdb
    txn and pin the bytes."""
    import struct as _struct

    import lmdb as _lmdb
    import block_store as _bs

    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        _commit(s, [_block(h, ntx=2) for h in (1, 2)])
    finally:
        s.close()

    env = _lmdb.open(str(tmp_path / "bs"), subdir=True, max_dbs=len(_bs.SUBDBS), readonly=True, lock=False)
    blocks = env.open_db(b"blocks"); hashes = env.open_db(b"hashes")
    pk = env.open_db(b"pk"); pkr = env.open_db(b"pkr")
    with env.begin() as txn:
        # height keys are big-endian uint64
        k1 = _struct.pack(">Q", 1)
        raw = txn.get(k1, db=blocks)
        assert raw is not None
        rec = _bs._unpack(raw)                       # same msgpack codec the store uses
        assert rec["h"] == "hash%08d" % 1
        assert len(rec["t"]) == 2
        # pubkey id is dedup'd: tx public-key field replaced by a small int id (0/1)
        assert rec["t"][0][BlockStore._PK] in (0, 1)
        # hashes: block_hash bytes -> BE height
        assert txn.get(b"hash%08d" % 1, db=hashes) == k1
        # pk table keyed by blake2b-32(public_key); pkr maps id -> raw pubkey bytes
        import hashlib as _hl
        hkey0 = _hl.blake2b(b"pubkey0", digest_size=32).digest()
        assert txn.get(hkey0, db=pk) is not None
        nid = _struct.unpack(">Q", txn.get(hkey0, db=pk))[0]
        assert txn.get(_struct.pack(">Q", nid), db=pkr) == b"pubkey0"
    env.close()


# --------------------------------------------------------------------------- hf2 Stage-4 true-bytes wiring
import base64

import addrbytes
import sigbytes


import amounts
import bismuth_serialize as _B


def _row_real(h, i, bh, addr, recip, sig, amount="0.50000000", fee="0.01000000", reward="0.00000000"):
    """A 12-field ledger row with realistic address/recipient/signature AND canonical '%.8f' money strings
    (so the Stage-4 codecs pack every field and the round-trip stays byte-identical)."""
    return [h, "%.2f" % (1600000000 + h), addr, recip, amount, sig,
            "pubkey%d" % i, bh, fee, reward, "op%d" % i, "openfield_%d_%d" % (h, i)]


def _rsa_addr(seed):
    # 56 lowercase hex (RSA / 0x00 family), DISTINCT per seed: seed in the high nibbles, zero-padded, so the
    # [:56] truncation keeps it distinct (a right-aligned "%064x" would collide for all small seeds).
    return ("%x" % seed).ljust(56, "0")[:56]


def _eight(r):  # the 8-field consensus tuple from a 12-field row (ts,addr,recip,amount,sig,pk,op,openfield)
    return (r[1], r[2], r[3], r[4], r[5], r[6], r[10], r[11])


def test_postfork_truebytes_roundtrip_realistic(tmp_path):
    """Post-fork (height >= fork_height) packs signature/address/recipient/timestamp/amount/fee/reward to
    TRUE BYTES; get_block rebuilds the exact 12-field rows byte-for-byte (canonical money strings)."""
    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        bh = "%064x" % 50                                      # valid 64-hex blake2b-style hash
        addr = _rsa_addr(0xAB)
        recip = _rsa_addr(0xCD)
        sig = base64.b64encode(b"\x07" * 64).decode()          # canonical base64 (RSA-family wire)
        rows = [_row_real(50, 0, bh, addr, recip, sig, amount="1.23456789"),
                _row_real(50, 1, bh, addr, recip, base64.b64encode(b"\x09" * 128).decode(), amount="0.00000001")]
        s.commit_block(50, bh, rows, fork_height=40)           # 50 >= 40 -> post-fork

        assert s.get_block_raw(50) == rows                        # byte-identical for canonical inputs
        assert s.block_hash(50) == bh                          # envelope hash reconstructs to hex

        # txrec consolidation: each tx is ONE bytes blob (not an 11-field msgpack list), envelope hash raw
        with s.store.txn() as txn:
            rec = block_store._unpack(txn.get(s.blocks, block_store._hk(50)))
        t0 = rec["t"][0]
        assert isinstance(t0, (bytes, bytearray))     # a single txrec blob per tx (consolidated)
        assert isinstance(rec["h"], (bytes, bytearray)) and len(rec["h"]) == 32  # envelope hash: raw 32B
        # the blob decodes back to the exact row fields (pubkey as the dedup id), and packs the scheme tags
        import txrec
        dec = txrec.unpack_row(bytes(t0))
        assert dec[0] == rows[0][1] and dec[3] == "1.23456789"      # timestamp, amount
        assert isinstance(dec[1], int) and isinstance(dec[2], int)  # address/recipient = "a"-dict indices
        assert dec[6] is None                          # block_hash NOT stored per tx (hoisted to envelope)
        # intra-block address dedup: 2 txs share the same addr+recip -> "a" has exactly 2 entries (not 4)
        assert len(rec["a"]) == 2
        assert s.get_block(50)[0][7] == bh             # _expand resolves addresses + fills block_hash
        # consolidated blob is far smaller than a per-field msgpack list of the same row
        legacy = block_store._pack(rows[0][1:])
        assert len(block_store._pack(t0)) < len(legacy)
    finally:
        s.close()


def test_postfork_amount_zero_normalization_consensus_safe(tmp_path):
    """A coinbase-shape row (amount/fee '0') normalizes to '0.00000000' on read — NOT byte-identical, but
    consensus-EQUIVALENT: same units, and the v2 block hash over the reconstructed row is identical."""
    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        bh = "hash%08d" % 60
        addr, recip = _rsa_addr(0x11), _rsa_addr(0x22)
        sig = base64.b64encode(b"\x05" * 64).decode()
        orig = _row_real(60, 0, bh, addr, recip, sig, amount="0", fee="0", reward="5.00000000")
        s.commit_block(60, bh, [orig], fork_height=40)
        got = s.get_block_raw(60)[0]

        assert got[4] == "0.00000000"                          # '0' normalized (consensus-safe)
        assert got[8] == "0"  or got[8] == "0.00000000"        # reward kept ('5.00000000' here)
        assert got[9] == "5.00000000"
        assert amounts.to_units(got[4]) == amounts.to_units(orig[4]) == 0
        # THE consensus invariant: v2 block hash over reconstructed == over original (form-independent)
        assert _B.block_hash_v2([_eight(got)], "00" * 32) == _B.block_hash_v2([_eight(orig)], "00" * 32)
    finally:
        s.close()


def test_postfork_straddling_and_fallback(tmp_path):
    """A pre-fork block stores legacy values (byte-identical); the next, post-fork block packs. Both reconstruct.
    Synthetic non-canonical address/sig still round-trip via the verbatim/opaque fallback."""
    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        # pre-fork block (height 5 < fork_height 6): legacy path, untouched
        _, bh5, rows5 = _block(5, ntx=2)
        s.commit_block(5, bh5, rows5, fork_height=6)
        assert s.get_block_raw(5) == rows5
        with s.store.txn() as txn:
            rec5 = block_store._unpack(txn.get(s.blocks, block_store._hk(5)))
        assert isinstance(rec5["t"][0][4], str)       # legacy: signature stays a str
        assert isinstance(rec5["t"][0][3], float)     # legacy: amount stays the original float

        # post-fork block: synthetic addr/sig take the verbatim/opaque fallback; canonical money packs
        bh6 = "hash%08d" % 6
        rows6 = [[6, "%.2f" % (1600000006), "addr%d" % i, "recip%d" % i, "0.50000000",
                  "sig_%d" % i, "pubkey%d" % i, bh6, "0.01000000", "0.00000000",
                  "op%d" % i, "of_%d" % i] for i in range(2)]
        s.commit_block(6, bh6, rows6, fork_height=6)
        assert s.get_block_raw(6) == rows6             # fallback + canonical money round-trip byte-for-byte
    finally:
        s.close()


def test_postfork_address_ref_dedup(tmp_path):
    """Intra-block address dedup: a self-spend (recipient == sender) plus a second tx from the same sender
    collapse to a tiny per-block "a" dict; get_block still reconstructs the exact rows."""
    s = BlockStore(str(tmp_path / "bs"), map_size=SMALL)
    try:
        bh = "%064x" % 70
        a = _rsa_addr(0x1)          # sender
        b = _rsa_addr(0x2)          # a distinct recipient
        sig = base64.b64encode(b"\x07" * 64).decode()
        rows = [
            _row_real(70, 0, bh, a, a, sig),                 # self-spend: recipient == sender
            _row_real(70, 1, bh, a, b, sig),                 # same sender, new recipient
        ]
        s.commit_block(70, bh, rows, fork_height=40)
        assert s.get_block_raw(70) == rows                      # exact reconstruction
        with s.store.txn() as txn:
            rec = block_store._unpack(txn.get(s.blocks, block_store._hk(70)))
        # only TWO distinct addresses across the block (a, b) -> "a" dict has 2 entries, not 4
        assert len(rec["a"]) == 2
    finally:
        s.close()
