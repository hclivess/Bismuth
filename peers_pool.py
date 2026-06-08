"""Outbound connection-pool membership and retry/back-off bookkeeping (mixin)."""
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

        # A node must never dial ITSELF. The default config ships 127.0.0.1 in peers.txt AND whitelists
        # it, so without this guard a node connects to its own listening socket, "syncs" from itself,
        # and records its own height as a consensus vote — inflating consensus to 100% off a single
        # self-opinion when it has no real peers, which looks like "fully synced" while stuck. Skip our
        # own listening address (public node_ip or loopback, on our own port); other local services on a
        # different port are still allowed.
        if str(port) == str(self.config.port) and host in ("127.0.0.1", "localhost", self.config.node_ip):
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

        # Back-off between retries to the same peer. The old schedule (30s, 5min, 15min, 30min) made a
        # catching-up node stall for many minutes: with only a handful of live peers, it churned through
        # them and then could not re-attempt for 5-30 min. This gentler, capped schedule (30s, 1m, 2m,
        # then 5m) still spaces retries politely but recovers from connection churn in about a minute.
        delay_map = {0: 30, 1: 60, 2: 120}
        delay = delay_map.get(tries, 5*60)

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
        host_port = f"{host}:{port}" if port else host
        self.tried.pop(host_port, None)  # pop-with-default never raises

    def reset_tried(self, aggressive=False):
        """
        Drop per-peer retry back-off timers so known peers become dial-eligible again.

        Two modes:

        * steady-low (``aggressive=False``, the default): drop only timers that are more than a short
          retry window out. This is called when we are connection-starved (few outbound peers), exactly
          when we most need to re-attempt known peers. The old window was 12 minutes, which *kept* the
          multi-minute back-offs that cause catch-up stalls; a 60s window means a starved node
          re-attempts essentially every peer within a minute instead of waiting out a long back-off,
          while peers cooling down for only a few more seconds are left undisturbed.

        * isolation recovery (``aggressive=True``): drop EVERY timer. This is the fast-recovery path for
          when outbound connections have collapsed to ~0 (e.g. just after a restart, or after losing all
          peers): we must not wait out *any* back-off, so we clear the whole tried dict and let the dial
          loop re-attempt all known peers immediately, in parallel up to the existing thread cap. A
          single restart therefore can't strand the whole live-peer set behind escalating cooldowns.
        """
        if aggressive:
            self.tried = {}
            return
        soon = time() + 60
        self.tried = {client: data for client, data in self.tried.items()
                      if data[1] <= soon}
