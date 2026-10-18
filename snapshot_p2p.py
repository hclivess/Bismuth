"""doc/43 — peer-to-peer ledger snapshot (decentralized rapid bootstrap), DB-DIRECT from the LMDB ledger.

The ledger is ONE LMDB environment (``block_store.BlockStore``); a snapshot is that whole env. It is
generated ON DEMAND straight from the live database and streamed to the requester — no pre-built file, no
operator cron, and no doubled copy of the ledger on disk. LMDB's ``env.copyfd`` writes an MVCC-consistent,
compacted image directly to the response socket, consistent even while the node keeps writing. Both halves
are OFF by default:

  * SERVE  (``node.snapshot_serve``): ``GET /api/snapshot/info`` reports ``{db_direct, height, tip_hash}``
    read O(1) from the live ledger's tip; ``GET /api/snapshot`` streams the env image via copyfd.
  * FETCH  (``node.bootstrap_p2p``): a fresh node picks the highest-height peer, streams the env into its
    own ledger dir (``kvstore.store_path(ledger_path, "blockstore")``), and verifies tip height + tip
    block_hash before trusting it.

Untrusted-source safety: the tip height + tip block_hash check fast-rejects corruption/forgery, and the
downloaded ledger is STILL fully re-validated by the digester as the node syncs forward — that full
re-validation is the real forgery backstop.
"""
import json
import os
import urllib.request


def _ledger(node):
    """The node's ledger ``BlockStore`` (``node.ledger``; ``node.block_store`` is the same object)."""
    return getattr(node, "ledger", None) or getattr(node, "block_store", None)


# --- DB-direct serve (doc/43 stage 2: on-demand, no pre-built file) --------------------------------
def db_snapshot_info(node):
    """The manifest for a DB-DIRECT snapshot served straight from this node's live LMDB ledger, or ``None``
    if the node has no ledger open yet (or it is empty) — then there is nothing to serve. Reads only the
    store's tip — O(1), no scan, no file. The tip block_hash lets a fetcher fast-reject a truncated/forged
    stream before the digester's full forward re-validation."""
    bs = _ledger(node)
    if bs is None:
        return None
    try:
        tip = bs.tip()
        if tip is None:
            return None
        tip_hash = bs.block_hash(tip)
    except Exception:
        return None
    if tip_hash is None:
        return None
    return {"available": True, "db_direct": True, "format": "lmdb",
            "height": int(tip), "tip_hash": tip_hash}


def stream_db_snapshot(node, fileobj):
    """Stream the live ledger env as a consistent, compacted MVCC image to ``fileobj`` (the response
    socket) — no pre-built file, no doubled copy on disk. The caller MUST have flushed any buffered
    bytes (HTTP headers) on ``fileobj`` first; copyfd writes the raw fd."""
    _ledger(node).stream_snapshot(fileobj)


# --- fetch side (a bootstrapping node pulling from peers) ------------------------------------------
def _get_json(url, timeout):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _download(url, dest, timeout):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    os.replace(tmp, dest)
    return dest


def candidates(node):
    """(host, rest_port) peers to try. Explicit ``bootstrap_p2p_peers`` (["host:port", ...]) wins; otherwise
    best-effort from the known peers paired with this node's REST port (a homogeneous fleet)."""
    out = []
    for e in (getattr(node, "bootstrap_p2p_peers", None) or []):
        s = str(e)
        if ":" in s:
            h, p = s.rsplit(":", 1)
            try:
                out.append((h, int(p)))
            except ValueError:
                pass
    if out:
        return out
    port = int(getattr(node, "rest_api_port", 0) or 0)
    if port:
        peers = getattr(node, "peers", None)
        try:
            ips = list(getattr(peers, "peer_dict", {}).keys()) if peers else []
        except Exception:
            ips = []
        out = [(ip, port) for ip in ips[:20]]
    return out


def _discard_dir(dest_dir):
    try:
        import shutil
        shutil.rmtree(dest_dir)
    except OSError:
        pass


def _verify_db_snapshot(dest_dir, info, log=None):
    """Reopen the streamed env and confirm its tip height + tip block_hash match the advertised manifest.
    Fast-rejects a corrupt/truncated/wrong-tip stream; the digester's forward re-validation remains the
    forgery backstop. Returns True iff the store opens and the tip matches."""
    try:
        from block_store import BlockStore
    except Exception:
        return False
    try:
        bs = BlockStore(dest_dir, readonly=True, sync=False)
    except Exception as e:
        if log:
            log.warning(f"Status: DB-direct snapshot won't open ({e}) — discarding")
        return False
    try:
        tip = bs.tip()
        ok = (tip is not None and int(tip) == int(info["height"])
              and bs.block_hash(tip) == info.get("tip_hash"))
        if not ok and log:
            log.warning("Status: DB-direct snapshot tip/height/hash mismatch — discarding")
        return bool(ok)
    except Exception:
        return False
    finally:
        bs.close()


def fetch_db_from_peers(node, cands, dest_dir, timeout=300, info_timeout=8):
    """Query each peer's ``/api/snapshot/info``, pick the highest-height DB-direct (LMDB) snapshot, stream
    ``/api/snapshot`` into ``<dest_dir>/data.mdb`` (the whole ledger env), and VERIFY it (tip height + tip block_hash) by reopening the store.
    Returns ``(dest_dir, info)`` on success, else ``None``. Never raises — a bad peer is skipped and a
    failed/verify-mismatch download discards the dir. The digester still re-validates the chain forward."""
    log = getattr(getattr(node, "logger", None), "app_log", None)
    best = None
    for host, port in cands:
        try:
            info = _get_json(f"http://{host}:{port}/api/snapshot/info", info_timeout)
        except Exception:
            continue
        if not (info and info.get("available") and info.get("db_direct")
                and info.get("tip_hash") and info.get("height") is not None):
            continue
        if best is None or int(info["height"]) > int(best[2]["height"]):
            best = (host, port, info)
    if not best:
        return None
    host, port, info = best
    if log:
        log.warning(f"Status: DB-direct P2P snapshot from {host}:{port} (height {info['height']}) — "
                    f"streaming + verifying")
    _discard_dir(dest_dir)
    os.makedirs(dest_dir)
    try:
        _download(f"http://{host}:{port}/api/snapshot", os.path.join(dest_dir, "data.mdb"), timeout)
    except Exception:
        _discard_dir(dest_dir)
        return None
    if not _verify_db_snapshot(dest_dir, info, log):
        _discard_dir(dest_dir)
        return None
    return dest_dir, info
