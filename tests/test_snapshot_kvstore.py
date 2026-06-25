"""
snapshot tool migration onto the engine-agnostic KV seam (scripts/snapshot.py -> kvstore.open_store +
KVStore.copy_to, doc/26 storage stage 1).

The snapshot tool used to call ``lmdb.open(...).copy(dst, compact=True)`` directly. It now routes through
``open_store(backend, src, readonly=True, lock=False).copy_to(dst)`` — the new uniform online-copy seam
primitive that both the LMDB backend (MVCC env.copy) and the sqlite-kv backend (online backup API)
implement. These tests prove:

  (a) ROUND-TRIP — copy_to produces a consistent, byte-readable copy: every key/value reopens identical to
      the source, on BOTH backends.
  (b) SWAPPABILITY — the SAME store snapshotted via the seam yields identical contents on lmdb vs sqlite-kv.
  (c) the snapshot_lmdb() tool entrypoint itself works end-to-end through the seam on a tmp store (no
      live node, tmp_path only) and the copied LMDB store reopens with identical bytes.

Run with: python3 -m pytest tests/test_snapshot_kvstore.py -v
"""
import struct

import pytest

pytest.importorskip("lmdb")
import lmdb  # noqa: E402

from kvstore import open_store  # noqa: E402

_MS = 64 * 1024 * 1024


def _populate(backend, path):
    """Write a small, ordered, multi-sub-db workload through the seam and return the written contents."""
    s = open_store(backend, path, dbs=["blocks", "pubkeys"], map_size=_MS)
    contents = {"blocks": {}, "pubkeys": {}}
    try:
        with s.txn(write=True) as t:
            blocks_db = s.open_db("blocks")
            pubkeys_db = s.open_db("pubkeys")
            for h in (5, 1, 9, 3, 7):
                k, v = struct.pack(">Q", h), b"block-%d-payload" % h
                t.put(blocks_db, k, v)
                contents["blocks"][k] = v
            for i in range(4):
                k, v = b"pk%d" % i, struct.pack(">Q", i)
                t.put(pubkeys_db, k, v)
                contents["pubkeys"][k] = v
    finally:
        s.close()
    return contents


def _read_all(backend, path):
    s = open_store(backend, path, dbs=["blocks", "pubkeys"], map_size=_MS, readonly=True)
    out = {"blocks": {}, "pubkeys": {}}
    try:
        for name in ("blocks", "pubkeys"):
            db = s.open_db(name)
            with s.txn() as t:
                for k, v in t.iterate(db):
                    out[name][bytes(k)] = bytes(v)
    finally:
        s.close()
    return out


@pytest.mark.parametrize("backend", ["lmdb", "sqlite-kv"])
def test_copy_to_roundtrip(backend, tmp_path):
    """(a) copy_to yields a consistent copy whose every key/value reopens identical to the source."""
    src = str(tmp_path / ("src_" + backend))
    dst = str(tmp_path / ("dst_" + backend))
    written = _populate(backend, src)

    s = open_store(backend, src, dbs=["blocks", "pubkeys"], map_size=_MS, readonly=True, lock=False)
    try:
        s.copy_to(dst, compact=True)
    finally:
        s.close()

    assert _read_all(backend, dst) == written


def test_copy_to_swappability_lmdb_equals_sqlite(tmp_path):
    """(b) the SAME workload snapshotted through the seam reads back identically on both backends."""
    res = {}
    for backend in ("lmdb", "sqlite-kv"):
        src = str(tmp_path / ("src_" + backend))
        dst = str(tmp_path / ("dst_" + backend))
        _populate(backend, src)
        s = open_store(backend, src, dbs=["blocks", "pubkeys"], map_size=_MS, readonly=True, lock=False)
        try:
            s.copy_to(dst, compact=True)
        finally:
            s.close()
        res[backend] = _read_all(backend, dst)
    assert res["lmdb"] == res["sqlite-kv"], "snapshot copy differs across backends -> seam not engine-independent"


def test_snapshot_lmdb_tool_entrypoint(tmp_path):
    """(c) the snapshot_lmdb() tool entrypoint copies an LMDB store through the seam; the copy reopens with
    byte-identical raw key/values read straight out of LMDB (proves on-disk format preserved). tmp_path only,
    no live node."""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts"))
    import snapshot

    src = str(tmp_path / "blockstore")
    written = _populate("lmdb", src)

    dst = str(tmp_path / "snap")
    snapshot.snapshot_lmdb(src, dst, backend="lmdb")

    # read raw bytes straight out of the COPIED lmdb env (not via the seam) — byte-identity proof
    env = lmdb.open(dst, subdir=True, max_dbs=2, readonly=True, lock=False)
    raw = {"blocks": {}, "pubkeys": {}}
    for name in ("blocks", "pubkeys"):
        db = env.open_db(name.encode())
        with env.begin() as t:
            for k, v in t.cursor(db=db):
                raw[name][bytes(k)] = bytes(v)
    env.close()
    assert raw == written
