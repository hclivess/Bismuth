"""
REST client for peer-to-peer block fetching over HTTP — the modern, parallel alternative to the
serial, blocking socket sync in ``connections.py``.

Two things a node does with a peer's REST API:

  1. **Capability discovery.** Fetch ``GET /api/capabilities``. If it answers, the peer is
     REST-capable and we learn its real REST port + negotiable codecs; if it's unreachable the peer
     simply isn't REST-capable ("if the API is inaccessible, it doesn't exist") and we fall back to
     the socket protocol.
  2. **Parallel block fetch.** Pull ``GET /api/blocks/range/{start}/{end}`` in concurrent chunks with
     gzip transport compression, so catch-up latency is the slowest chunk rather than the sum of a
     serial walk. This is the performance fix for block getting.

Stdlib only (``urllib`` + ``ThreadPoolExecutor``) — no aiohttp/requests dependency. Fails soft: any
HTTP/parse error returns ``None`` (discovery) or raises a clear error (fetch) so the caller can fall
back to socket sync rather than stalling.
"""
import concurrent.futures
import gzip
import json
import urllib.request

DEFAULT_TIMEOUT = 10
DEFAULT_CHUNK = 100      # blocks per HTTP request
DEFAULT_WORKERS = 8      # concurrent requests


def base_url(host, port):
    return "http://{}:{}/api".format(host, int(port))


def _get_json(url, timeout=DEFAULT_TIMEOUT):
    """GET ``url`` advertising gzip, transparently decompress, and parse JSON."""
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def get_capabilities(host, port, timeout=DEFAULT_TIMEOUT):
    """A peer's capability dict, or ``None`` if its REST API is unreachable (=> not REST-capable)."""
    try:
        return _get_json(base_url(host, port) + "/capabilities", timeout=timeout)
    except Exception:
        return None


def is_rest_capable(host, port, timeout=DEFAULT_TIMEOUT):
    """True iff the peer serves the REST API — reachability is itself the capability test."""
    return get_capabilities(host, port, timeout=timeout) is not None


def get_height(host, port, timeout=DEFAULT_TIMEOUT):
    """The peer's current block height (from /api/status), or ``None`` if unreachable."""
    try:
        return _get_json(base_url(host, port) + "/status", timeout=timeout).get("blocks")
    except Exception:
        return None


def fetch_range(host, port, start, end, timeout=DEFAULT_TIMEOUT):
    """Blocks in the inclusive ``[start, end]`` height range as a list of
    ``{"block_height", "transactions"}`` dicts, ascending. Raises on HTTP/parse error."""
    data = _get_json("{}/blocks/range/{}/{}".format(base_url(host, port), int(start), int(end)),
                     timeout=timeout)
    return data.get("blocks", [])


def parallel_fetch(host, port, start, end, chunk=DEFAULT_CHUNK, workers=DEFAULT_WORKERS,
                   timeout=DEFAULT_TIMEOUT):
    """Fetch ``[start, end]`` as concurrent chunks and return all blocks sorted by height.

    Wall-clock is the slowest chunk, not the serial sum — the win over socket sync. A chunk that
    fails propagates its exception (the caller decides whether to retry that span or fall back)."""
    if end < start:
        return []
    spans = [(s, min(s + chunk - 1, end)) for s in range(start, end + 1, chunk)]
    blocks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(spans)))) as pool:
        for chunk_blocks in pool.map(lambda se: fetch_range(host, port, se[0], se[1], timeout), spans):
            blocks.extend(chunk_blocks)
    blocks.sort(key=lambda b: b["block_height"])
    return blocks
