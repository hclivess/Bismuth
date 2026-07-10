"""Peer access control: bans, weighted warnings, whitelist, and mainnet-version gating (mixin)."""


class PeersAccessMixin:
    __slots__ = ()

    def store_mainnet(self, ip, version):
        """Stores the mainnet version of a peer. Can't change unless reconnects"""
        with self.peers_lock:
            self.ip_to_mainnet[ip] = version

    def forget_mainnet(self, ip):
        """Peers disconnected, forget his mainnet version"""
        with self.peers_lock:
            self.ip_to_mainnet.pop(ip, None)

    def version_allowed(self, ip, version_allow):
        """
        If we don't know the version for this ip, allow.
        If we know, check
        """
        if ip not in self.ip_to_mainnet:
            return True
        return self.ip_to_mainnet[ip] in version_allow

    def unban(self, peer_ip):
        """Removes the peer_ip from the warning list"""
        with self.peers_lock:
            if peer_ip in self._warning_counts:  # Optimization: use Counter
                del self._warning_counts[peer_ip]
                # Also clean from warning_list for compatibility
                self.warning_list = [ip for ip in self.warning_list if ip != peer_ip]
                self.app_log.warning(f"Removed a warning for {peer_ip}")

    def warning(self, sdef, ip, reason, count):
        """Adds a weighted warning to a peer."""
        if ip not in self.whitelist:
            # read-modify-write of the warning counter + banlist under one lock: concurrent warnings for
            # the same ip must not lose a count or double-ban.
            with self.peers_lock:
                self._warning_counts[ip] += count       # Optimization: Use Counter instead of list
                for _ in range(count):                  # Maintain warning_list for compatibility
                    self.warning_list.append(ip)
                current_warnings = self._warning_counts[ip]
                banned = current_warnings >= self.ban_threshold and ip not in self.banlist
                if banned:
                    self.banlist.append(ip)
            self.app_log.warning(f"Added {count} warning(s) to {ip}: {reason} "
                               f"({current_warnings} / {self.ban_threshold})")
            if banned:
                self.app_log.warning(f"{ip} is banned: {reason}")
            return current_warnings >= self.ban_threshold

    def is_allowed(self, peer_ip, command=''):
        """Tells if the given peer is allowed for that command"""
        # Optimization: Early returns for common cases
        if command == 'block' and self.is_whitelisted(peer_ip):
            return True
        if command == 'portget':
            return True
        if command in ('stop', 'addpeers'):
            return peer_ip == '127.0.0.1'
        return peer_ip in self.config.allowed or "any" in self.config.allowed

    def is_whitelisted(self, peer_ip, command=''):
        return peer_ip in self.whitelist or peer_ip == "127.0.0.1"

    def is_banned(self, peer_ip) -> bool:
        return peer_ip in self.banlist
