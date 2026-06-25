"""Peer-file disk I/O and inbound peer-list sync for the ``Peers`` manager (mixin)."""
import json
import os
import shutil

import socks

import connections


class PeersStorageMixin:
    __slots__ = ()

    def peers_test(self, file, peerdict: dict, strict=True):
        """Validates then adds a peer to the peer list on disk"""
        # Optimization: Early exit and batch processing
        self.peerlist_updated = False
        try:
            with open(file, "r") as peer_file:
                peers_pairs = json.load(peer_file)

            # Optimization: Pre-filter peers to test
            peers_to_test = [(ip, port) for ip, port in peerdict.items()
                            if ip not in peers_pairs]

            if not peers_to_test:
                self.app_log.warning(f"{file} peerlist update skipped, no new peers")
                return

            # Batch test peers
            for ip, port in peers_to_test:
                if self.node.IS_STOPPING:
                    return

                try:
                    self.app_log.info(f"Testing connectivity to: {ip}:{port}")
                    s = socks.socksocket()
                    try:
                        s.settimeout(5)
                        _tm = getattr(self.node, "tor_manager", None)   # doc/38: single proxy source; None on clearnet
                        _proxy = _tm.get_proxy() if _tm is not None else None
                        if _proxy:
                            s.setproxy(socks.PROXY_TYPE_SOCKS5, _proxy[0], _proxy[1])
                        if strict:
                            s.connect((ip, int(port)))
                            connections.send(s, "getversion")
                            versiongot = connections.receive(s, timeout=1)
                            if versiongot == "*":
                                raise ValueError("peer busy")
                            if versiongot not in self.config.version_allow:
                                raise ValueError(f"cannot save {ip}, incompatible protocol version {versiongot} "
                                               f"not in {self.config.version_allow}")
                            self.app_log.info(f"Inbound: Distant peer {ip}:{port} responding: {versiongot}")
                        else:
                            s.connect((ip, int(port)))
                    finally:
                        try:
                            s.close()
                        except:
                            pass
                    peers_pairs[ip] = port
                    self.app_log.info(f"Inbound: Peer {ip}:{port} saved to peers")
                    self.peerlist_updated = True

                except Exception as e:
                    self.app_log.info(f"Inbound: Distant peer not connectible ({e})")

            if self.peerlist_updated:
                self.app_log.warning(f"{file} peerlist updated ({len(peers_pairs)}) total")
                # Optimization: Use atomic write
                with open(f"{file}.tmp", "w") as peer_file:
                    json.dump(peers_pairs, peer_file)
                shutil.move(f"{file}.tmp", file)

        except Exception as e:
            self.app_log.info(f"Error reading {file}: '{e}'")

    def peers_get(self, peer_file=''):
        """Returns a peer_file from disk as a dict {ip:port}"""
        peer_dict = {}
        try:
            if not peer_file:
                peer_file = self.peerfile
            if not os.path.exists(peer_file):
                with open(peer_file, "w") as fp:
                    self.app_log.warning("Peer file created")
                    fp.write("{}")
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
                total_added = 0
                subdata = self.dict_validate(subdata)
                data_dict = json.loads(subdata)

                self.app_log.info(f"Received {len(data_dict)} peers.")

                # Optimization: Batch process new peers
                new_peers = {ip: port for ip, port in data_dict.items()
                           if ip not in self.peer_dict}

                for ip, port in new_peers.items():
                    self.app_log.info(f"Outbound: {ip}:{port} is a new peer, saving if connectible")
                    try:
                        s_purge = socks.socksocket()
                        try:
                            s_purge.settimeout(5)
                            _tm = getattr(self.node, "tor_manager", None)   # doc/38: single proxy source; None on clearnet
                            _proxy = _tm.get_proxy() if _tm is not None else None
                            if _proxy:
                                s_purge.setproxy(socks.PROXY_TYPE_SOCKS5, _proxy[0], _proxy[1])
                            s_purge.connect((ip, int(port)))
                        finally:
                            # Probe socket must be closed even when connect() raises (the common
                            # unreachable-peer case), or we leak a file descriptor every peersync.
                            try:
                                s_purge.close()
                            except Exception:
                                pass

                        if ip not in self.peer_dict:
                            total_added += 1
                            self.peer_dict[ip] = port
                            self.app_log.info(f"Inbound: Peer {ip}:{port} saved to local peers")
                    except:
                        self.app_log.info("Not connectible")
            except Exception as e:
                self.app_log.warning(f"peersync failed: {type(e).__name__}: {e}")
                raise
        return total_added
