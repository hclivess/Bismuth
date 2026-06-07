"""
Headers-first, capability-gated chain-segment fetch over the REST API.

This is the safe seam between ``rest_client`` (HTTP transport) and the digester (consensus): given a
peer and a height range, it returns **digester-ready** blocks — but only after confirming the peer is
REST-capable, that the header chain is contiguous, and that every downloaded body corresponds to the
header that was advertised. Anything off → it returns ``None`` (fail-soft) so the caller falls back to
the legacy socket sync rather than stalling or ingesting a partial/inconsistent segment.

Division of responsibility: this layer validates the *transport* (capability, contiguity, body↔header
correspondence); the *consensus* validation (PoW, signatures, balance, hash linkage) stays in the
digester, which ingests whatever this returns exactly as it would socket-delivered blocks.

No live mainnet peer is REST-capable yet, so today this lets a node SERVE fast sync and is the path
the network takes once peers upgrade; the catch-up-loop wiring (calling this, then ``digest_block``
on the result, behind a default-off flag) needs a two-node harness and is deliberately separate.
"""
import rest_client


def sync_segment(host, rest_port, start, end, timeout=rest_client.DEFAULT_TIMEOUT):
    """Fetch + validate blocks ``[start, end]`` from a peer's REST API as digester-ready data.

    Returns a list of blocks (each a list of consensus tx tuples, ready for ``digest_block``) on
    success, or ``None`` to signal the caller should fall back to socket sync. Never raises.
    """
    if end < start:
        return None
    try:
        if not rest_client.is_rest_capable(host, rest_port, timeout=timeout):
            return None  # peer has no REST API — not a fast-sync source

        # headers-first: pull the cheap header chain and require it to be exactly contiguous
        headers = rest_client.fetch_headers(host, rest_port, start, end, timeout=timeout)
        if not rest_client.headers_are_contiguous(headers, start) or headers[-1]["block_height"] != end:
            return None
        hash_by_height = {h["block_height"]: h["block_hash"] for h in headers}

        # bodies in parallel; must cover the exact range and match the headers we committed to
        blocks = rest_client.parallel_fetch_sync(host, rest_port, start, end, timeout=timeout)
        if [b["block_height"] for b in blocks] != list(range(start, end + 1)):
            return None
        for b in blocks:
            if b.get("block_hash") != hash_by_height.get(b["block_height"]):
                return None  # body the peer sent does not match the header it advertised

        return rest_client.blocks_to_digester(blocks)
    except Exception:
        return None  # fail-soft: any transport/parse error -> fall back to socket sync


def best_rest_source(candidates, timeout=rest_client.DEFAULT_TIMEOUT):
    """From ``[(host, rest_port), …]`` return the reachable REST peer with the highest advertised
    height (the best fast-sync source), or ``None`` if none are REST-capable."""
    best, best_h = None, -1
    for host, port in candidates:
        h = rest_client.get_height(host, port, timeout=timeout)
        if h is not None and h > best_h:
            best, best_h = (host, port), h
    return best
