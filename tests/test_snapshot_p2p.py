"""doc/43 — P2P snapshot interface: manifest + serve/fetch + sha256 verification.

Spins a tiny stub HTTP server that mimics a peer's /api/snapshot[/info] and proves a fetching node
downloads + verifies the tarball, and rejects a tampered one. No real node needed.
"""
import http.server
import json
import os
import threading

import snapshot_p2p as sp


def _make_tarball(tmp_path, data=b"FAKE-LEDGER-TARBALL" * 1000):
    tar = os.path.join(tmp_path, "ledger-snapshot.tar.gz")
    with open(tar, "wb") as f:
        f.write(data)
    return tar


def test_manifest_roundtrip_and_stale_guard(tmp_path):
    tar = _make_tarball(str(tmp_path))
    m = sp.write_manifest(tar, height=4872361)
    assert m["height"] == 4872361 and len(m["sha256"]) == 64 and m["size"] == os.path.getsize(tar)
    got = sp.read_manifest(tar)
    assert got == m
    # stale guard: if the tarball changes but the manifest doesn't, read_manifest refuses it
    with open(tar, "ab") as f:
        f.write(b"more")
    assert sp.read_manifest(tar) is None


def _serve(manifest, tarball_bytes):
    """Start a stub peer; returns (host, port, stop())."""
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/api/snapshot/info":
                body = json.dumps({"available": True, **manifest}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            elif self.path == "/api/snapshot":
                self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(tarball_bytes))); self.end_headers()
                self.wfile.write(tarball_bytes)
            else:
                self.send_response(404); self.end_headers()

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "127.0.0.1", srv.server_address[1], srv.shutdown


class _Node:
    logger = None


def test_fetch_from_peers_verifies(tmp_path):
    data = b"FAKE-LEDGER" * 5000
    tar = _make_tarball(str(tmp_path), data)
    man = sp.write_manifest(tar, height=100)
    host, port, stop = _serve(man, data)
    try:
        dest = os.path.join(str(tmp_path), "downloaded.tar.gz")
        got = sp.fetch_from_peers(_Node(), [(host, port)], dest)
        assert got is not None, "fetch should succeed"
        _, info = got
        assert info["height"] == 100
        assert open(dest, "rb").read() == data            # byte-identical
        assert sp.sha256_file(dest) == man["sha256"]       # verified
    finally:
        stop()


def test_fetch_rejects_tampered(tmp_path):
    data = b"REAL" * 5000
    tar = _make_tarball(str(tmp_path), data)
    man = sp.write_manifest(tar, height=100)
    # peer advertises the real manifest (real sha256) but serves DIFFERENT bytes
    host, port, stop = _serve(man, b"TAMPERED" * 5000)
    try:
        dest = os.path.join(str(tmp_path), "bad.tar.gz")
        got = sp.fetch_from_peers(_Node(), [(host, port)], dest)
        assert got is None, "tampered tarball must be rejected on sha256 mismatch"
        assert not os.path.exists(dest), "rejected download must be discarded"
    finally:
        stop()


def test_fetch_picks_highest_height(tmp_path):
    data = b"X" * 4000
    tar = _make_tarball(str(tmp_path), data)
    low = dict(sp.write_manifest(tar, height=50))
    high = dict(low, height=200)
    h1, p1, s1 = _serve(low, data)
    h2, p2, s2 = _serve(high, data)
    try:
        dest = os.path.join(str(tmp_path), "d.tar.gz")
        got = sp.fetch_from_peers(_Node(), [(h1, p1), (h2, p2)], dest)
        assert got and got[1]["height"] == 200, "should pick the highest-height peer"
    finally:
        s1(); s2()
