"""doc/43 — peer-to-peer ledger snapshot (decentralized rapid bootstrap).

The central bootstrap (``bootstrap_url`` -> bismuth.cz/ledger.tar.gz) is a single point of failure; this
adds the same rapid full-ledger bootstrap, but served BY PEERS. Two halves, both OFF by default:

  * SERVE  (``node.snapshot_serve``): a node exposes a pre-built snapshot tarball over REST —
    ``GET /api/snapshot/info`` (manifest: height + sha256 + size) and ``GET /api/snapshot`` (the bytes).
    The serve path only ever streams an ALREADY-BUILT file; it NEVER reads or scans the live ledger on a
    request (scripts/snapshot.py builds the tarball + this manifest out-of-band, consistently).
  * FETCH  (``node.bootstrap_p2p``): a fresh node asks peers for ``/api/snapshot/info``, picks the
    highest-height snapshot, downloads it, and VERIFIES the sha256 before trusting it (chain_ops.bootstrap).

The sha256 makes the source untrusted-safe: a peer cannot feed a corrupt/forged tarball undetected, and the
downloaded ledger is still fully re-validated by the digester as the node syncs forward from the snapshot.
"""
import hashlib
import json
import os
import urllib.request

MANIFEST_SUFFIX = ".manifest.json"


def manifest_path(tarball):
    return tarball + MANIFEST_SUFFIX


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def write_manifest(tarball, height):
    """Write the sidecar manifest next to a freshly built snapshot tarball. Returns the manifest dict."""
    m = {"height": int(height), "sha256": sha256_file(tarball), "size": os.path.getsize(tarball),
         "tarball": os.path.basename(tarball)}
    tmp = manifest_path(tarball) + ".part"
    with open(tmp, "w") as f:
        json.dump(m, f)
    os.replace(tmp, manifest_path(tarball))
    return m


def read_manifest(tarball):
    """The manifest for a present, non-stale snapshot tarball, or None. Guards against a stale manifest by
    requiring the recorded size to match the tarball on disk (so a half-rebuilt snapshot is never served)."""
    mp = manifest_path(tarball)
    if not (os.path.exists(tarball) and os.path.exists(mp)):
        return None
    try:
        with open(mp) as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    if not (m.get("sha256") and m.get("height") is not None):
        return None
    if int(m.get("size", -1)) != os.path.getsize(tarball):
        return None
    return m


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
    best-effort from the known peers paired with this node's REST port (a homogeneous post-hf2 fleet)."""
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


def fetch_from_peers(node, cands, dest, timeout=30, info_timeout=8):
    """Query each (host, port)'s /api/snapshot/info, pick the highest-height available snapshot, download
    /api/snapshot to ``dest``, and verify its sha256. Returns (dest, manifest) on success, else None.
    Never raises — a bad peer is skipped; a hash mismatch discards the file and returns None."""
    log = getattr(getattr(node, "logger", None), "app_log", None)
    best = None
    for host, port in cands:
        try:
            info = _get_json(f"http://{host}:{port}/api/snapshot/info", info_timeout)
        except Exception:
            continue
        if not (info and info.get("available") and info.get("sha256") and info.get("height") is not None):
            continue
        if best is None or int(info["height"]) > int(best[2]["height"]):
            best = (host, port, info)
    if not best:
        return None
    host, port, info = best
    if log:
        log.warning(f"Status: P2P snapshot from {host}:{port} (height {info['height']}, "
                    f"{int(info.get('size', 0)) / 1e6:.0f} MB) — downloading + verifying")
    try:
        _download(f"http://{host}:{port}/api/snapshot", dest, timeout)
    except Exception:
        return None
    if sha256_file(dest) != info["sha256"]:
        if log:
            log.warning("Status: P2P snapshot sha256 MISMATCH — discarding")
        try:
            os.remove(dest)
        except OSError:
            pass
        return None
    return dest, info
