"""doc/43 stage 2 — DB-DIRECT snapshot serving (on-demand, no pre-built file, no doubled ledger copy).

Proves the automated/no-double-files path end to end WITHOUT a real node:
  * BlockStore.stream_snapshot streams a consistent, compacted MVCC env image (env.copyfd) that reopens
    block-for-block identical — no tarball, no intermediate file built by an operator.
  * The stream is a point-in-time snapshot: mutating the source AFTER streaming does not change the copy.
  * snapshot_p2p.db_snapshot_info reports the live tip + tip_hash O(1).
  * Over a real HTTP socket (mirroring rest_api's serve handler: flush headers, then copyfd to the socket),
    snapshot_p2p.fetch_db_from_peers streams it into a fresh block_store dir, VERIFIES tip+hash, picks the
    highest-height peer, and rejects a peer whose advertised tip_hash doesn't match the bytes it serves.

Run with: python3 -m pytest tests/test_snapshot_db_direct.py -v
"""
import http.server
import os
import threading

import pytest

pytest.importorskip("lmdb")  # DB-direct streaming is LMDB-only (env.copyfd)

import snapshot_p2p as sp
from block_store import BlockStore

SMALL = 64 * 1024 * 1024  # 64 MiB map is plenty for tests


def _row(h, i, bh):
    return [h, "%.2f" % (1600000000 + h), "addr%d" % i, "recip%d" % i, 0.5 + i,
            "sig_%d_%d" % (h, i), "pubkey%d" % i, bh, 0.01, 1.0 if i == 0 else 0,
            "op%d" % i, "openfield_%d_%d" % (h, i)]


def _block(h, ntx=2):
    bh = "hash%08d" % h
    return h, bh, [_row(h, i, bh) for i in range(ntx)]


def _populate(path, heights):
    s = BlockStore(path, map_size=SMALL)
    s.put_blocks([_block(h) for h in heights])
    return s


class _Node:
    """Minimal stand-in for the parts of node snapshot_p2p touches."""
    logger = None

    def __init__(self, block_store=None):
        self.block_store = block_store


def test_stream_snapshot_reopens_identical(tmp_path):
    src = _populate(str(tmp_path / "src"), [1, 2, 3, 7, 8])
    try:
        dest = tmp_path / "dst"
        dest.mkdir()
        with open(dest / "data.mdb", "wb") as f:
            src.stream_snapshot(f)            # env.copyfd straight to the file — no tarball
    finally:
        src.close()
    copy = BlockStore(str(dest), map_size=SMALL, readonly=True, sync=False)
    try:
        assert copy.tip() == 8
        assert copy.count() == 5
        assert [h for h, _ in copy.blocks_in_range(1, 8)] == [1, 2, 3, 7, 8]
        assert copy.get_block(7) == _block(7)[2]          # block-for-block identical
        assert copy.block_hash(8) == "hash00000008"
    finally:
        copy.close()


def test_stream_is_point_in_time(tmp_path):
    """Mutating the source after streaming must not change the already-streamed copy (no doubling, no
    leak of later writes) — the consistency guarantee env.copyfd provides while the node keeps writing."""
    src = _populate(str(tmp_path / "src"), [1, 2, 3])
    dest = tmp_path / "dst"
    dest.mkdir()
    try:
        with open(dest / "data.mdb", "wb") as f:
            src.stream_snapshot(f)
        src.put_blocks([_block(h) for h in (4, 5, 6)])     # writer races on after the snapshot
        assert src.tip() == 6
    finally:
        src.close()
    copy = BlockStore(str(dest), map_size=SMALL, readonly=True, sync=False)
    try:
        assert copy.tip() == 3, "streamed copy must be the point-in-time view, not see later writes"
    finally:
        copy.close()


def test_db_snapshot_info(tmp_path):
    src = _populate(str(tmp_path / "src"), [1, 2, 3, 4, 5])
    try:
        info = sp.db_snapshot_info(_Node(src))
        assert info == {"available": True, "db_direct": True, "format": "lmdb",
                        "height": 5, "tip_hash": "hash00000005"}
    finally:
        src.close()
    assert sp.db_snapshot_info(_Node(None)) is None      # no block store -> fall back to tarball path


def _serve(node, tamper_tip_hash=None):
    """Stub peer mirroring rest_api._send_snapshot / _snapshot_info for the DB-direct path."""
    import json

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/api/snapshot/info":
                info = dict(sp.db_snapshot_info(node))
                if tamper_tip_hash is not None:
                    info["tip_hash"] = tamper_tip_hash    # advertise a tip_hash the bytes won't match
                body = json.dumps(info).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/snapshot":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()                        # headers before the raw copyfd stream
                sp.stream_db_snapshot(node, self.wfile)
                self.close_connection = True
            else:
                self.send_response(404)
                self.end_headers()

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "127.0.0.1", srv.server_address[1], srv.shutdown


def test_fetch_db_from_peers_roundtrip(tmp_path):
    src = _populate(str(tmp_path / "src"), [1, 2, 3, 4, 9])
    host, port, stop = _serve(_Node(src))
    try:
        dest = str(tmp_path / "fetched")
        got = sp.fetch_db_from_peers(_Node(), [(host, port)], dest, timeout=30)
        assert got is not None, "DB-direct fetch should succeed"
        _, info = got
        assert info["height"] == 9 and info["db_direct"] is True
        copy = BlockStore(dest, map_size=SMALL, readonly=True, sync=False)
        try:
            assert copy.tip() == 9
            assert copy.get_block(9) == _block(9)[2]      # streamed straight from the live DB, identical
        finally:
            copy.close()
    finally:
        stop()
        src.close()


def test_fetch_db_picks_highest_and_rejects_tampered(tmp_path):
    low = _populate(str(tmp_path / "low"), [1, 2])
    high = _populate(str(tmp_path / "high"), [1, 2, 3, 4])
    h1, p1, s1 = _serve(_Node(low))
    h2, p2, s2 = _serve(_Node(high))
    try:
        dest = str(tmp_path / "fetched")
        got = sp.fetch_db_from_peers(_Node(), [(h1, p1), (h2, p2)], dest, timeout=30)
        assert got and got[1]["height"] == 4, "should pick the highest-height peer"
    finally:
        s1(); s2(); low.close(); high.close()

    # a peer that advertises a tip_hash its streamed bytes won't satisfy must be rejected + discarded
    bad_src = _populate(str(tmp_path / "bad"), [1, 2, 3])
    hb, pb, sb = _serve(_Node(bad_src), tamper_tip_hash="deadbeef" * 8)
    try:
        dest2 = str(tmp_path / "rejected")
        got = sp.fetch_db_from_peers(_Node(), [(hb, pb)], dest2, timeout=30)
        assert got is None, "tip_hash mismatch must be rejected"
        assert not os.path.exists(dest2), "rejected download must be discarded"
    finally:
        sb(); bad_src.close()
