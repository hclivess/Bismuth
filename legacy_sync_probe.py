"""
Read-only legacy-peer compatibility probe.

Answers one question: can THIS codebase still talk to the live mainnet legacy clients?
It replicates node.py / worker.py's outbound handshake and sync negotiation against real peers
using the node's own connections.send/receive wire protocol — but it is strictly read-only: it
never sends a transaction, never mines, never writes a ledger. It poses as a node stuck at a known
historical height (from db_hashes.py) and tries to pull the next real blocks, then disconnects.

Usage:  python3 legacy_sync_probe.py
"""
import json
import socket
import sys
import time

from connections import send, receive
from db_hashes import db_hashes

# A known-good historical checkpoint from db_hashes.py: we tell the peer "my tip is block 27258
# with this hash" so it recognises us as a valid (but behind) node and offers us the next blocks.
KNOWN_HEIGHT = 27258
KNOWN_HASH = db_hashes['27258-1493755375.23']


def read_config_version(default="mainnet0022"):
    try:
        for line in open("config.txt"):
            line = line.strip()
            if line.startswith("version="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return default


def reachable_peers(limit=12):
    peers = json.load(open("peers.txt"))
    out = []
    for host, port in peers.items():
        if host == "127.0.0.1":
            continue
        try:
            s = socket.create_connection((host, int(port)), timeout=5)
            s.close()
            out.append((host, int(port)))
        except Exception:
            pass
        if len(out) >= limit:
            break
    return out


def probe(host, port, version, version_allow):
    log = []
    def L(m): log.append(m); print(f"  [{host}] {m}", flush=True)
    try:
        s = socket.create_connection((host, port), timeout=8)
    except Exception as e:
        L(f"TCP connect FAILED: {e}")
        return False, log
    result = {"handshake": False, "peer_version": None, "peer_height": None, "got_block": False}
    try:
        # --- handshake (worker.py:72-87) ---
        send(s, "version")
        send(s, version)
        data = receive(s, timeout=10)
        if data != "ok":
            L(f"version handshake REJECTED (got {data!r})")
            return False, log
        L("version handshake -> 'ok'  ✓ peer accepts our protocol")
        send(s, "getversion")
        peer_version = receive(s, timeout=10)
        result["peer_version"] = peer_version
        L(f"peer version = {peer_version!r}  ({'compatible' if peer_version in version_allow else 'NOT in our allow-list'})")
        send(s, "hello")
        result["handshake"] = True

        # --- drive the receive loop for a bounded window (worker.py:111+) ---
        deadline = time.time() + 25
        while time.time() < deadline:
            data = receive(s, timeout=12)
            if data == "*":
                L("(idle timeout waiting for peer command)")
                continue
            L(f"peer -> {str(data)[:40]!r}")
            if data == "peers":
                subdata = receive(s, timeout=10)
                L(f"received peer list ({len(subdata) if hasattr(subdata,'__len__') else '?'} peers)")
            elif data == "sync":
                # we initiate the height comparison, posing as a node at KNOWN_HEIGHT
                send(s, "blockheight")
                send(s, KNOWN_HEIGHT)
                peer_height = receive(s, timeout=12)
                result["peer_height"] = peer_height
                L(f"HEIGHT NEGOTIATION ✓ peer is at block {peer_height} (we claimed {KNOWN_HEIGHT})")
                try:
                    if int(peer_height) >= KNOWN_HEIGHT:
                        # send our 'last block hash' so the peer offers us the next blocks
                        send(s, KNOWN_HASH)
                except Exception as e:
                    L(f"(height compare note: {e})")
            elif data == "blocksfnd":
                L("peer says it FOUND blocks for us -> confirming")
                send(s, "blockscf")
                segments = receive(s, timeout=20)
                if isinstance(segments, list) and segments:
                    first = segments[0]
                    h = first[0] if isinstance(first, list) else "?"
                    L(f"RECEIVED {len(segments)} real tx/block segments from legacy peer (first block_height={h})  ✓✓ FULL SYNC PATH WORKS")
                    result["got_block"] = True
                else:
                    L(f"got blocks reply: {str(segments)[:60]}")
                break
            elif data == "nonewblk":
                L("peer has no newer block for our claimed height")
                break
            elif data in ("blocknf", "blocknfhb"):
                receive(s, timeout=8)
                L("peer reports our claimed block not found on its chain (expected for an old checkpoint)")
                break
        s.close()
    except Exception as e:
        L(f"error mid-probe: {e}")
        try: s.close()
        except Exception: pass
    return result, log


def main():
    version = read_config_version()
    # accept any mainnet version back from peers
    version_allow = ["mainnet0021", "mainnet0022", "mainnet0023", "mainnet0020", "mainnet0019"]
    print(f"Our advertised version: {version}")
    print("Finding reachable mainnet peers...", flush=True)
    peers = reachable_peers()
    print(f"Reachable: {peers}\n", flush=True)
    summary = []
    for host, port in peers[:4]:
        print(f"=== probing {host}:{port} ===", flush=True)
        res, _ = probe(host, port, version, version_allow)
        summary.append((host, res))
        print(flush=True)
    print("================ SUMMARY ================")
    for host, res in summary:
        if not res:
            print(f"{host:18}  handshake FAILED")
            continue
        print(f"{host:18}  handshake={res['handshake']}  peer_version={res['peer_version']}  "
              f"peer_height={res['peer_height']}  received_real_blocks={res['got_block']}")


if __name__ == "__main__":
    main()
