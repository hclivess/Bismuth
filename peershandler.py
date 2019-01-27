"""
Peers handler module for Bismuth nodes
@EggPoolNet
"""

import json
import os
import re
import sys
import threading
import time

import socks

import regnet

__version__ = "0.0.12"


# TODO : some config options are _conf and others without => clean up later on

def most_common(lst: list):
    """Used by consensus"""
    # TODO: factorize the two helpers in one. and use a less cpu hungry method (counter)
    return max(set(lst), key=lst.count)

def most_common_dict(a_dict: dict):
    """Returns the most common value from a dict. Used by consensus"""
    return max(a_dict.values())

def percentage_in(individual, whole):
    return (float(list(whole).count(individual) / float(len(whole)))) * 100


class Peers:
    """The peers manager. A thread safe peers manager"""

    __slots__ = ('app_log','config','logstats','node','peersync_lock','startup_time','reset_time','warning_list','stats',
                 'connection_pool','peer_opinion_dict','consensus_percentage','consensus',
                 'tried','peer_dict','peerfile','suggested_peerfile','banlist','whitelist','ban_threshold',
                 'ip_to_mainnet', 'peers', 'consensus_lock', 'first_run', 'accept_peers')

    def __init__(self, app_log, config=None, logstats=True, node=None):
        self.app_log = app_log
        self.config = config
        self.logstats = logstats

        self.peersync_lock = threading.Lock()
        self.consensus_lock = threading.Lock()
        self.startup_time = time.time()
        self.reset_time = self.startup_time
        self.warning_list = []
        self.stats = []
        self.connection_pool = []
        self.peer_opinion_dict = {}
        self.consensus_percentage = 0
        self.consensus = None
        self.tried = {}
        self.peer_dict = {}
        self.ip_to_mainnet = {}
        self.connection_pool = []
        # We store them apart from the initial config, could diverge somehow later on.
        self.banlist = config.banlist
        self.whitelist = config.whitelist
        self.ban_threshold = config.ban_threshold
        self.accept_peers = config.accept_peers

        self.peerfile = "peers.txt"
        self.suggested_peerfile = "suggested_peers.txt"
        self.first_run = True

        self.node = node

        if self.is_testnet:  # overwrite for testnet
            self.peerfile = "peers_test.txt"
            self.suggested_peerfile = "suggested_peers_test.txt"

        if self.is_regnet:  # regnet won't use any peer, won't connect. Kept for compatibility
            self.peerfile = regnet.REGNET_PEERS
            self.suggested_peerfile = regnet.REGNET_SUGGESTED_PEERS

    @property
    def is_testnet(self):
        """Helper to check if testnet or not. Only one place to change variable names and test"""
        if self.config.regnet:
            # regnet takes over testnet
            return False
        if self.config.testnet:
            return True
        return "testnet" in self.config.version_conf

    @property
    def is_regnet(self):
        """Helper to check if regnet or not. Only one place to change variable names and test"""
        if self.config.regnet:
            # regnet takes over testnet
            return True
        return "regnet" in self.config.version_conf

    def status_dict(self):
        """Returns a status as a dict"""
        status = {"version": self.config.VERSION, "stats": self.stats}
        return status

    def store_mainnet(self, ip, version):
        """Stores the mainnet version of a peer. Can't change unless reconnects"""
        self.ip_to_mainnet[ip] = version

    def forget_mainnet(self, ip):
        """Peers disconnected, forget his mainnet version"""
        self.ip_to_mainnet.pop(ip, None)

    def version_allowed(self, ip, version_allow):
        """
        If we don't know the version for this ip, allow.
        If we know, check
        """
        if ip not in self.ip_to_mainnet:
            return True
        return self.ip_to_mainnet[ip] in version_allow

    def peer_dump(self, file, peer):
        """saves single peer to drive"""
        with open(file, "r") as peer_file:
            peers_pairs = json.load(peer_file)
            peers_pairs[peer] = self.config.port
        with open(file, "w") as peer_file:
            json.dump(peers_pairs, peer_file)

    def peers_dump(self, file, peerdict):
        """Validates then adds a peer to the peer list on disk"""
        # called by Sync, should not be an issue, but check if needs to be thread safe or not.

        with open(file, "r") as peer_file:
            peers_pairs = json.load(peer_file)

        for ip in peerdict:
            peer_ip = ip
            peer_port = self.config.port

            try:
                if peer_ip not in peers_pairs:
                    self.app_log.warning(f"Testing connectivity to: {peer_ip}")
                    peer_test = socks.socksocket()
                    if self.config.tor_conf == 1:
                        peer_test.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                    peer_test.connect((str(peer_ip), int(self.config.port)))  # double parentheses mean tuple
                    self.app_log.info("Inbound: Distant peer connectible")

                    # properly end the connection
                    peer_test.close()
                    # properly end the connection

                    peers_pairs[ip] = peer_port

                    with open(file, "w") as peer_file:
                        json.dump(peers_pairs, peer_file)

                    self.app_log.info(f"Inbound: Peer {peer_ip}:{peer_port} saved to peer list")

                else:
                    self.app_log.info("Distant peer already in peer list")
            except:
                self.app_log.info("Inbound: Distant peer not connectible")
                pass

    def append_client(self, client):
        """
        :param client: a string "ip:port"
        :return:
        """
        # TODO: thread safe?
        self.connection_pool.append(client)
        self.del_try(client)

    def remove_client(self, client):
        # TODO: thread safe?
        self.connection_pool.remove(client)

    def unban(self, peer_ip):
        """Removes the peer_ip from the warning list"""
        # TODO: Not thread safe atm. Should use a thread aware list or some lock
        if peer_ip in self.warning_list:
            self.warning_list.remove(peer_ip)
            self.app_log.warning(f"Removed a warning for {peer_ip}")

    def warning(self, sdef, ip, reason, count):
        """Adds a weighted warning to a peer."""
        # TODO: Not thread safe atm. Should use a thread aware list or some lock
        if ip not in self.whitelist:
            # TODO: use a dict instead of several occurences in a list
            for x in range(count):
                self.warning_list.append(ip)
            self.app_log.warning(f"Added {count} warning(s) to {ip}: {reason} ({self.warning_list.count(ip)} / {self.ban_threshold})")

            if self.warning_list.count(ip) >= self.ban_threshold:
                self.banlist.append(ip)
                self.app_log.warning(f"{ip} is banned: {reason}")
                return True
            else:
                return False

    def peers_get(self, peerfile=''):
        """Returns a peerfile from disk as a dict {ip:port}"""
        peer_dict = {}
        if not peerfile:
            peerfile = self.peerfile
        if not os.path.exists(peerfile):
            with open(peerfile, "a"):
                self.app_log.warning("Peer file created")
        else:
            with open(peerfile, "r") as peer_file:
                peer_dict = json.load(peer_file)
        return peer_dict

    def peer_list_disk_format(self):
        """Returns a peerfile as is, simple text format or json, as it is on disk"""
        # TODO: caching and format to handle here
        with open(self.peerfile, "r") as peer_list:
            peers = peer_list.read()
        return peers

    @property
    def consensus_most_common(self):
        """Consensus vote"""
        try:
            return most_common_dict(self.peer_opinion_dict)
        except:
            # no consensus yet
            return 0

    @property
    def consensus_max(self):
        try:
            return max(self.peer_opinion_dict.values())
        except:
            # no consensus yet
            return 0

    @property
    def consensus_size(self):
        """Number of nodes in consensus"""
        return len(self.peer_opinion_dict)

    def is_allowed(self, peer_ip, command=''):
        """Tells if the given peer is allowed for that command"""
        # TODO: more granularity here later
        # Always allow whitelisted ip to post as block
        if 'block' == command and self.is_whitelisted(peer_ip):
            return True
        # only allow local host for "stop" command
        if 'stop' == command:
            return peer_ip == '127.0.0.1'
        return peer_ip in self.config.allowed_conf or "any" in self.config.allowed_conf

    def is_whitelisted(self, peer_ip, command=''):
        # TODO: could be handled later on via "allowed" and rights.
        return peer_ip in self.whitelist or "127.0.0.1" == peer_ip

    def is_banned(self, peer_ip):
        return peer_ip in self.banlist

    def peers_test(self, peerfile):
        """Tests all peers from a list."""
        # TODO: lengthy, no need to test everyone at once?
        if not self.peersync_lock.locked() and self.config.accept_peers:
            self.peersync_lock.acquire()
            try:
                peer_dict = self.peers_get(peerfile)
                peers_remove = {}

                for key, value in peer_dict.items():
                    host, port = key, int(value)
                    try:
                        s = socks.socksocket()
                        s.settimeout(0.6)
                        if self.config.tor_conf == 1:
                            s.settimeout(5)
                            s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                        s.connect((host, port))
                        s.close()
                        self.app_log.info(f"Connection to {host} {port} successful, keeping the peer")
                    except:
                        if self.config.purge_conf == 1 and not self.is_testnet:
                            # remove from peerfile if not connectible

                            peers_remove[key] = value
                        pass

                for key in peers_remove:
                    del peer_dict[key]
                    self.app_log.info(f"Removed formerly active peer {key}")

                with open(peerfile, "w") as output:
                    json.dump(peer_dict, output)
            finally:
                self.peersync_lock.release()

    def peersync(self, subdata):
        """Got a peers list from a peer, process. From worker()."""

        # early exit to reduce future levels
        if not self.config.accept_peers:
            return
        if self.peersync_lock.locked():
            self.app_log.info("Outbound: Peer sync occupied")
            return
        self.peersync_lock.acquire()
        try:
            if "(" in str(subdata):  # OLD WAY
                server_peer_tuples = re.findall("'([\d.]+)', '([\d]+)'", subdata)

                self.app_log.info(f"Received following {len(server_peer_tuples)} peers: {server_peer_tuples}")
                with open(self.peerfile, "r") as peer_file:
                    peers = json.load(peer_file)

                for pair in set(server_peer_tuples):  # set removes duplicates
                    if pair not in peers and self.accept_peers:
                        self.app_log.info(f"Outbound: {pair} is a new peer, saving if connectible")
                        try:
                            s_purge = socks.socksocket()
                            if self.config.tor_conf == 1:
                                s_purge.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)

                            s_purge.connect((pair[0], int(pair[1])))  # save a new peer file with only active nodes
                            s_purge.close()
                            # suggested

                            with open(self.suggested_peerfile) as peers_existing:
                                peers_suggested = json.load(peers_existing)
                                if pair not in peers_suggested:
                                    peers_suggested[pair[0]] = pair[1]

                                    with open(self.suggested_peerfile, "w") as peer_list_file:
                                        json.dump(peers_suggested, peer_list_file)
                            # suggested
                            peers[pair[0]] = pair[1]
                            with open(self.peerfile, "w") as peer_file:
                                json.dump(peers, peer_file)

                        except:
                            pass
                            self.app_log.info("Not connectible")
                    else:
                        self.app_log.info(f"Outbound: {pair} is not a new peer")

            else:
                # json format
                self.app_log.info(f"Received following {len(json.loads(subdata))} peers: {subdata}")

                for ip,port in json.loads(subdata).items():

                    if ip not in self.peer_dict.keys():
                        self.app_log.info(f"Outbound: {ip}:{port} is a new peer, saving if connectible")
                        try:
                            s_purge = socks.socksocket()
                            if self.config.tor_conf == 1:
                                s_purge.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                            s_purge.connect((ip, int(port)))  # save a new peer file with only active nodes
                            s_purge.close()

                            if ip not in self.peer_dict.keys():
                                self.peer_dict[ip] = port

                        except:
                            pass
                            self.app_log.info("Not connectible")
                    else:
                        self.app_log.info(f"Outbound: {ip}:{port} is not a new peer")
        finally:
            self.peersync_lock.release()

    def consensus_add(self, peer_ip, consensus_blockheight, sdef, last_block):
        # obviously too old blocks, we have half a day worth of validated blocks after them
        # no ban, they can (should) be syncing but they can't possibly be in consensus list.
        too_old = last_block - 720
        try:
            self.consensus_lock.acquire()

            if peer_ip not in self.peer_opinion_dict:
                if consensus_blockheight < too_old:
                    self.app_log.warning(f"{peer_ip} got too old a block ({consensus_blockheight}) for consensus")
                    return

            self.app_log.info(f"Updating {peer_ip} in consensus")
            self.peer_opinion_dict[peer_ip] = consensus_blockheight

            self.consensus = most_common_dict(self.peer_opinion_dict)

            self.consensus_percentage = percentage_in(self.peer_opinion_dict[peer_ip],self.peer_opinion_dict.values())

            if int(consensus_blockheight) > int(self.consensus) + 30 and self.consensus_percentage > 50 and len(self.peer_opinion_dict) > 10:
                if self.warning(sdef, peer_ip, "Consensus deviation too high", 10):
                    raise ValueError("{} banned".format(peer_ip))

        except Exception as e:
            self.app_log.warning(e)
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            raise
        finally:
            self.consensus_lock.release()

    def consensus_remove(self, peer_ip):
        self.consensus_lock.acquire()
        try:
            self.app_log.info(f"Consensus opinion list: {self.peer_opinion_dict}")
            self.app_log.info(f"Will remove {peer_ip} from consensus pool {self.peer_opinion_dict}")
            self.peer_opinion_dict.pop(peer_ip)
        except:
            self.app_log.info(f"IP of {peer_ip} not present in the consensus pool")
            pass
        finally:
            self.consensus_lock.release()

    def can_connect_to(self, host, port):

        """
        Tells if we can connect to this host
        :param host:
        :param port:
        :return:
        """
        if host in self.banlist:
            return False  # Banned IP
        host_port = f"{host}:{port}"
        if host_port in self.connection_pool:
            return False  # Already connected to
        try:
            tries, timeout = self.tried[host_port]
        except:
            tries, timeout = 0, 0  # unknown host for now, never tried.
        if timeout > time.time():
            return False  # We tried before, timeout is not expired.
        if self.is_whitelisted(host):
            return True  # whitelisted peers are always connectible, without variability condition.
        # variability test.
        c_class = '.'.join(host.split('.')[:-1]) + '.'
        matching = [ip_port for ip_port in self.connection_pool if c_class in ip_port]
        # If we already have 2 peers from that C ip class in our connection pool, ignore.
        if len(matching) >= 2:
            # Temp debug
            self.app_log.warning(f"Ignoring {host_port} since we already have 2 ips of that C Class in our pool.")
            return False
        # Else we can
        return True

    def add_try(self, host, port):
        """
        Add the host to the tried dict with matching timeout depending on its state.
        :param host:
        :param port:
        :return:
        """
        host_port = host + ":" + str(port)
        try:
            tries, timeout = self.tried[host_port]
        except:
            tries, timeout = 0, 0
        if tries <= 0:  # First time can be temp, retry again
            delay = 30
        elif tries == 1:  # second time, give it 5 minutes
            delay = 5*60
        elif tries == 2:  # third time, give it 15 minutes
            delay = 15 * 60
        else:  # 30 minutes before trying again
            delay = 30*60
        tries += 1
        if tries > 3:
            tries = 3
        self.tried[host_port] = (tries, time.time() + delay)
        # Temp
        self.app_log.info(f"Set timeout {delay} try {tries} for {host_port}")

    def del_try(self, host, port=None):
        """
        Remove the peer from tried list. To be called when we successfully connected.
        :param host: an ip as a string, or an "ip:port" string
        :param port: optional, port as an int
        :return:
        """
        try:
            if port:
                host_port = host + ":" + str(port)
            else:
                host_port = host
            if host_port in self.tried:
                del self.tried[host_port]
        except Exception as e:
            print(e)
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)

    def reset_tried(self):
        """
        Remove the older timeouts from the tried list.
        Keep the recent ones or we end up trying the first ones again and again
        """
        limit = time.time() + 12*60  # matches 2.5 tries :)
        remove = [client for client in self.tried if self.tried[client][1] > limit]
        for client in remove:
            del self.tried[client]

    def client_loop(self, node, target):
        """Manager loop called every 30 sec. Handles maintenance"""
        try:
            for key, value in self.peer_dict.items():
                host = key
                port = int(value)

                if self.is_testnet:
                    port = 2829
                if threading.active_count()/3 < self.config.thread_limit_conf and self.can_connect_to(host, port):
                    self.app_log.info(f"Will attempt to connect to {host}:{port}")
                    self.add_try(host, port)
                    t = threading.Thread(target=target, args=(host, port, node))  # threaded connectivity to nodes here
                    self.app_log.info(f"---Starting a client thread {threading.currentThread()} ---")
                    t.daemon = True
                    t.start()

            if len(self.peer_dict) < 3 and int(time.time() - self.startup_time) > 15:
                # join in random peers after x seconds
                self.app_log.warning("Not enough peers in consensus, joining in peers suggested by other nodes")
                self.peer_dict.update(self.peers_get(self.suggested_peerfile))

            if len(self.connection_pool) < self.config.nodes_ban_reset and int(time.time() - self.startup_time) > 15:
                # do not reset before 30 secs have passed
                self.app_log.warning(f"Only {len(self.connection_pool)} connections active, resetting banlist")
                del self.banlist[:]
                self.banlist.extend(self.config.banlist)  # reset to config version
                del self.warning_list[:]

            if len(self.connection_pool) < 10:
                self.app_log.warning(f"Only {len(self.connection_pool)} connections active, resetting the connection history")
                # TODO: only reset large timeouts, or we end up trying the sames over and over if we never get to 10.
                # self.
                self.reset_tried()

            if self.config.nodes_ban_reset and len(self.connection_pool) <= len(self.banlist) \
                    and int(time.time() - self.reset_time) > 60*10:
                # do not reset too often. 10 minutes here
                self.app_log.warning(f"Less active connections ({len(self.connection_pool)}) than banlist ({len(self.banlist)}), resetting banlist and tried")
                del self.banlist[:]
                self.banlist.extend(self.config.banlist)  # reset to config version
                del self.warning_list[:]
                self.reset_tried()
                self.reset_time = time.time()

            if self.first_run and int(time.time() - self.startup_time) > 90:
                self.app_log.warning("Status: First run, testing peers")
                self.peers_test(self.peerfile)
                self.peers_test(self.suggested_peerfile)
                self.first_run = False

            #now moved after testing of peers
            if int(time.time() - self.startup_time) > 15:  # refreshes peers from drive
                self.peer_dict.update(self.peers_get(self.peerfile))

            self.peers_dump(self.suggested_peerfile,self.peer_dict)


        except Exception as e:
            self.app_log.warning(f"Status: Manager run skipped due to error: {e}")

    def status_log(self):
        """Prints the peers part of the node status"""
        if self.banlist:
            self.app_log.warning(f"Status: Banlist: {self.banlist}")
            self.app_log.warning(f"Status: Banlist Count : {len(self.banlist)}")
        if self.whitelist:
            self.app_log.warning(f"Status: Whitelist: {self.whitelist}")

        self.app_log.warning(f"Status: Known Peers: {len(self.peer_dict)}")
        self.app_log.info(f"Status: Tried: {self.tried}")
        self.app_log.info(f"Status: Tried Count: {len(self.tried)}")
        self.app_log.info(f"Status: List of Outbound connections: {self.connection_pool}")
        self.app_log.warning(f"Status: Number of Outbound connections: {len(self.connection_pool)}")
        if self.consensus:  # once the consensus is filled
            self.app_log.warning(f"Status: Consensus height: {self.consensus} = {self.consensus_percentage}%")
            self.app_log.warning(f"Status: Last block opinion: {self.peer_opinion_dict}")
            self.app_log.warning(f"Status: Total number of nodes: {len(self.peer_opinion_dict)}")
