# Transport-codec unit tests (no node). Run with: python3 -m pytest -v
# Proves every available compression codec round-trips bytes exactly and that negotiation picks a
# mutually-supported codec, falling back to 'none'. See transport.py / doc/06.

import json

import transport


def test_baseline_codecs_always_present():
    codecs = transport.available_codecs()
    assert "none" in codecs and "zlib" in codecs and "gzip" in codecs  # stdlib, always available


def test_every_available_codec_roundtrips():
    payload = json.dumps({"block_height": 12345, "transactions": [{"amount": "2.50000000"}] * 50,
                          "openfield": "x" * 4000}).encode("utf-8")
    for codec in transport.available_codecs():
        blob = transport.compress(codec, payload)
        assert transport.decompress(codec, blob) == payload, codec
        if codec not in ("none",):
            # a redundant payload should actually get smaller under a real compressor
            assert len(blob) < len(payload), codec


def test_negotiate_prefers_mutual_then_falls_back():
    # both speak zlib -> zlib chosen (preferred over gzip)
    assert transport.negotiate(["gzip", "zlib", "none"]) == "zlib"
    # remote only speaks an unknown codec -> 'none'
    assert transport.negotiate(["lzMADEUP"]) == "none"
    # remote speaks nothing -> 'none'
    assert transport.negotiate([]) == "none"
    assert transport.negotiate(None) == "none"


def test_unknown_codec_is_identity_safe():
    # An unknown codec must never raise on the hot path; it degrades to pass-through.
    assert transport.compress("nope", b"abc") == b"abc"
    assert transport.decompress("nope", b"abc") == b"abc"
