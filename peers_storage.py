"""Peer-file disk I/O and inbound peer-list sync for the ``Peers`` manager (mixin).

Connectivity probing here is CONCURRENT: both peers_test (persist path) and peersync (gossip
path) fan the per-peer blocking connect() out across a bounded thread pool, then mutate the
shared peer state ONCE from the calling thread. Previously each did serial 5s blocking probes
inside the single ConnectionManager thread, so a batch of dead peers stalled the whole
maintenance/dial loop for minutes (n x 5s). Now the wall-clock is ceil(n/PROBE_CONCURRENCY)x5s,
and — because the shared dict is written once after the pool drains rather than per-iteration —
there is no longer a torn-read window on peer_dict / the on-disk pairs during a probe batch.
"""
import json
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import socks

import connections

# Bounded fan-out for connectivity probes. Probes are pure I/O-wait (a 5s blocking connect, plus a
# short getversion handshake in strict mode), so a wide pool is cheap; the cap keeps a huge gossiped
# peerlist from spawning hundreds of short-lived sockets at once.
PROBE_CONCURRENCY = 24
PROBE_TIMEOUT = 5          # seconds; blocking connect() bound per peer (was inline 5)


class PeersStorageMixin:
    __slots__ = ()

    # --- crash-safe JSON write --------------------------------------------------------------------
    def _atomic_write_json(self, path, obj):
        """Durably replace ``path`` with ``obj`` as JSON: write a UNIQUE tmp in the same dir, flush +
        fsync it, then os.replace (atomic on the same filesystem). A crash can now only leave the old
        file or the fully-written new one — never the truncated/zero-length file a bare open('w') (or a
        move without fsync) could leave, which peers_get would then fail to parse and silently drop to
        {}. The tmp name is unique (mkstemp) so concurrent writers can't clobber each other's tmp."""
        d = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(obj, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            try:                                   # best-effort: fsync the dir so the rename survives a crash
                dfd = os.open(d, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # --- single-peer connectivity probe (runs in a pool thread) -----------------------------------
    def _probe_peer(self, ip, port, strict):
        """Return True if ``ip:port`` is connectible (and, in strict mode, speaks a compatible protocol
        version). Runs in a worker thread; opens/closes its OWN socket so it shares no mutable state —
        the caller aggregates the boolean results. Never raises: an unreachable peer is just False."""
        try:
            s = socks.socksocket()
            try:
                s.settimeout(PROBE_TIMEOUT)
                _tm = getattr(self.node, "tor_manager", None)   # doc/38: single proxy source; None on clearnet
                _proxy = _tm.get_proxy() if _tm is not None else None
                if _proxy:
                    s.setproxy(socks.PROXY_TYPE_SOCKS5, _proxy[0], _proxy[1])
                s.connect((ip, int(port)))
                if strict:
                    connections.send(s, "getversion")
                    versiongot = connections.receive(s, timeout=1)
                    if versiongot == "*":
                        raise ValueError("peer busy")
                    if versiongot not in self.config.version_allow:
                        raise ValueError(f"incompatible protocol version {versiongot} "
                                         f"not in {self.config.version_allow}")
                    self.app_log.info(f"Inbound: Distant peer {ip}:{port} responding: {versiongot}")
                return True
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        except Exception as e:
            self.app_log.info(f"Inbound: Distant peer {ip}:{port} not connectible ({e})")
            return False

    def _probe_many(self, candidates, strict):
        """Probe an iterable of (ip, port) CONCURRENTLY; return the list of connectible (ip, port).
        Honours IS_STOPPING (stops submitting / short-circuits pending results on shutdown)."""
        candidates = list(candidates)
        if not candidates:
            return []
        connectible = []
        workers = min(PROBE_CONCURRENCY, len(candidates))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="peerprobe") as pool:
            futures = {}
            for ip, port in candidates:
                if self.node.IS_STOPPING:
                    break
                futures[pool.submit(self._probe_peer, ip, port, strict)] = (ip, port)
            for fut in as_completed(futures):
                if self.node.IS_STOPPING:
                    break
                ip, port = futures[fut]
                try:
                    if fut.result():
                        connectible.append((ip, port))
                except Exception:
                    pass
        return connectible

    def peers_test(self, file, peerdict: dict, strict=True):
        """Validate then persist (to ``file``) every peer in ``peerdict`` not already saved. Probes run
        concurrently; the file is rewritten ONCE, atomically, only if at least one new peer validated."""
        self.peerlist_updated = False
        try:
            try:
                with open(file, "r") as peer_file:
                    peers_pairs = json.load(peer_file)
            except (json.JSONDecodeError, ValueError) as corrupt:
                # A corrupt/truncated peerfile must NOT silently zero the node's known-peer set forever.
                # Back it up (for diagnosis) and start from empty so the next successful probe re-persists
                # a clean file — self-healing instead of the old "swallow and return {}" that left the bad
                # file on disk until something happened to overwrite it.
                self.app_log.warning(f"{file} is corrupt ({corrupt}); backing up to {file}.corrupt and rebuilding")
                try:
                    shutil.copy(file, f"{file}.corrupt")
                except OSError:
                    pass
                peers_pairs = {}

            # peerdict is the live self.peer_dict (client_loop passes it in); snapshot under lock so the
            # comprehension can't hit "dictionary changed size during iteration" against a peer thread.
            with self.peers_lock:
                peerdict_snapshot = list(peerdict.items())
            peers_to_test = [(ip, port) for ip, port in peerdict_snapshot if ip not in peers_pairs]
            if not peers_to_test:
                self.app_log.info(f"{file} peerlist update skipped, no new peers")
                return

            for ip, port in self._probe_many(peers_to_test, strict):
                peers_pairs[ip] = port
                self.peerlist_updated = True
                self.app_log.info(f"Inbound: Peer {ip}:{port} saved to peers")

            if self.peerlist_updated:
                self.app_log.warning(f"{file} peerlist updated ({len(peers_pairs)}) total")
                self._atomic_write_json(file, peers_pairs)

        except Exception as e:
            self.app_log.info(f"Error reading {file}: '{e}'")

    def peers_get(self, peer_file=''):
        """Returns a peer_file from disk as a dict {ip:port}"""
        peer_dict = {}
        try:
            if not peer_file:
                peer_file = self.peerfile
            if not os.path.exists(peer_file):
                self.app_log.warning("Peer file created")
                self._atomic_write_json(peer_file, {})
            else:
                with open(peer_file, "r") as fp:
                    peer_dict = json.load(fp)
        except Exception as e:
            self.app_log.warning(f"Error peers_get {e} reading {peer_file}")
        return peer_dict

    def peer_list_disk_format(self):
        """Returns a peerfile as is, simple text format or json, as it is on disk"""
        with open(self.peerfile, "r") as peer_list:
            peers = peer_list.read()
        return peers

    def dict_validate(self, json_dict: str) -> str:
        """temporary fix for broken peerlists"""
        if json_dict.count("}") > 1:
            result = json_dict.split("}")[0] + "}"
        else:
            result = json_dict
        return result

    def peersync(self, subdata: str) -> int:
        """Got a peers list from a peer, process. From worker().
        returns the number of added peers, -1 if it was locked or not accepting new peers
        subdata is a dict, { 'ip': 'port'}"""
        if not self.config.accept_peers:
            return -1
        if self.peersync_lock.locked():
            self.app_log.info("Outbound: Peer sync occupied")
            return -1

        # Type enforcement
        if type(subdata) == dict:
            self.app_log.warning("Enforced expected type for peersync subdata")
            subdata = json.dumps(subdata)

        with self.peersync_lock:
            try:
                subdata = self.dict_validate(subdata)
                data_dict = json.loads(subdata)
                self.app_log.info(f"Received {len(data_dict)} peers.")

                # Only probe peers we don't already know. Probe CONCURRENTLY, then add the connectible
                # ones to peer_dict in one shot from this thread (single writer -> no torn state).
                new_peers = [(ip, port) for ip, port in data_dict.items() if ip not in self.peer_dict]
                connectible = self._probe_many(new_peers, strict=False)   # probes OUTSIDE peers_lock
                total_added = 0
                with self.peers_lock:                                     # only the dict mutation is locked
                    for ip, port in connectible:
                        if ip not in self.peer_dict:
                            self.peer_dict[ip] = port
                            total_added += 1
                            self.app_log.info(f"Inbound: Peer {ip}:{port} saved to local peers")
            except Exception as e:
                self.app_log.warning(f"peersync failed: {type(e).__name__}: {e}")
                raise
        return total_added
