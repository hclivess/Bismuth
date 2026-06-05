"""
Pluggable transport-layer compression for the node-to-node socket protocol (doc/06).

The legacy wire format (``connections.send`` / ``connections.receive``) is a fixed-length-prefixed
UTF-8 JSON payload. Compression is **negotiated, never assumed**: a node advertises the codecs it
supports through the ``getcapabilities`` handshake (see ``node.py``), the two ends pick the best
mutually-supported codec, and only then is the JSON payload compressed on that link. Peers too old to
speak ``getcapabilities`` keep receiving plain JSON, so this is fully backward compatible.

Codec availability is probed at import time so the node never *hard*-depends on a native library:

  - ``none``  — identity (the universal fallback; bytes pass through unchanged)
  - ``zlib``  — stdlib DEFLATE (always available; the baseline negotiated codec)
  - ``gzip``  — stdlib gzip wrapper (always available)
  - ``snappy``— Google Snappy, only if ``python-snappy`` is installed (fast, low ratio)
  - ``brotli``— Google Brotli, only if ``brotli`` is installed (slower, high ratio)

Note: nado's wire "compression" is really msgpack (a compact *serialization*, not a byte compressor);
that belongs at the serialization layer (replacing ``json``) and is intentionally out of scope here —
this module only compresses the already-serialized bytes.
"""
import gzip
import zlib

# codec name -> (compress(bytes) -> bytes, decompress(bytes) -> bytes)
_CODECS = {
    "none": (lambda b: b, lambda b: b),
    "zlib": (zlib.compress, zlib.decompress),
    "gzip": (gzip.compress, gzip.decompress),
}

# Optional native codecs: registered only if importable, keeping this a zero-hard-dependency module.
try:  # Google Snappy
    import snappy as _snappy

    _CODECS["snappy"] = (_snappy.compress, _snappy.decompress)
except Exception:  # pragma: no cover - depends on optional package
    pass

try:  # Google Brotli
    import brotli as _brotli

    _CODECS["brotli"] = (_brotli.compress, _brotli.decompress)
except Exception:  # pragma: no cover - depends on optional package
    pass

# Negotiation preference, best transport trade-off first; "none" is always the final fallback.
_PREFERENCE = ["snappy", "zlib", "gzip", "brotli", "none"]


def available_codecs():
    """Codec names this node can encode/decode, in negotiation-preference order."""
    return [name for name in _PREFERENCE if name in _CODECS]


def negotiate(remote_codecs):
    """Best codec supported by both this node and ``remote_codecs`` (preference order), else 'none'."""
    remote = set(remote_codecs or ())
    for name in _PREFERENCE:
        if name in _CODECS and name in remote:
            return name
    return "none"


def is_supported(codec):
    return codec in _CODECS


def compress(codec, raw):
    """Compress ``raw`` bytes with ``codec``. Unknown codec falls back to identity (safe)."""
    fn = _CODECS.get(codec)
    return raw if fn is None else fn[0](raw)


def decompress(codec, blob):
    """Inverse of :func:`compress`. Unknown codec falls back to identity (safe)."""
    fn = _CODECS.get(codec)
    return blob if fn is None else fn[1](blob)
