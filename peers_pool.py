"""Outbound connection-pool membership and retry/back-off bookkeeping (mixin)."""
import os
import sys
from time import time


class PeersPoolMixin:
    __slots__ = ()

    def append_client(self, client):
        """
        :param client: a string "ip:port"
        :return:
        """
        self.connection_pool.append(client)
        self._connection_pool_set.add(client)  # Optimization: maintain set
        self.del_try(client)

    def remove_client(self, client):
        if client in self._connection_pool_set:  # Optimization: O(1) lookup
            try:
                self.app_log.info(f"Will remove {client} from active pool")
                self.connection_pool.remove(client)
                self._connection_pool_set.discard(client)  # Optimization: maintain set
            except:
                raise

    def can_connect_to(self, host, port):
        """
        Tells if we can connect to this host
        :param host:
        :param port:
        :return:
        """
        # Optimization: Early exits for common cases
        if host in self.banlist:
            return False

        host_port = f"{host}:{port}"

        # Optimization: Use set for O(1) lookup
        if host_port in self._connection_pool_set:
            return False

        # Check timeout
        tries, timeout = self.tried.get(host_port, (0, 0))
        if timeout > time():
            return False

        if self.is_whitelisted(host):
            return True

        # Optimization: Cache C-class extraction
        if host not in self._c_class_cache:
            self._c_class_cache[host] = '.'.join(host.split('.')[:-1]) + '.'
        c_class = self._c_class_cache[host]

        # Optimization: Use generator expression for efficiency
        matching_count = sum(1 for ip_port in self._connection_pool_set if c_class in ip_port)

        if matching_count >= 2:
            self.app_log.warning(f"Ignoring {host_port} since we already have 2 ips of that C Class in our pool.")
            return False

        return True

    def add_try(self, host, port):
        """
        Add the host to the tried dict with matching timeout depending on its state.
        :param host:
        :param port:
        :return:
        """
        host_port = f"{host}:{port}"
        tries, _ = self.tried.get(host_port, (0, 0))

        # Optimization: Use lookup table for delays
        delay_map = {0: 30, 1: 5*60, 2: 15*60}
        delay = delay_map.get(tries, 30*60)

        tries = min(tries + 1, 3)
        self.tried[host_port] = (tries, time() + delay)
        self.app_log.info(f"Set timeout {delay} try {tries} for {host_port}")

    def del_try(self, host, port=None):
        """
        Remove the peer from tried list. To be called when we successfully connected.
        :param host: an ip as a string, or an "ip:port" string
        :param port: optional, port as an int
        :return:
        """
        try:
            host_port = f"{host}:{port}" if port else host
            self.tried.pop(host_port, None)  # Optimization: Use pop with default
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
        limit = time() + 12*60
        # Optimization: Dict comprehension instead of multiple deletions
        self.tried = {client: data for client, data in self.tried.items()
                     if data[1] <= limit}
