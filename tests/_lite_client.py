"""
Dependency-light Bismuth test client.

Replaces the external `bismuthclient` for the test-suite: it speaks the node wire protocol with
the in-tree ``connections`` module and signs transactions with the in-tree ``polysign`` (via
``essentials.sign_rsa``). No bismuthclient / bismuthcore / tornado / ed25519 required, so the suite
runs on any modern Python where the node itself runs.
"""
import socket
import time

import connections
import essentials


class LiteClient:
    def __init__(self, wallet_file, ip="127.0.0.1", port=3030, timeout=30):
        self.ip = ip
        self.port = int(port)
        self.timeout = timeout
        # keys_load_new -> (key, pub_readable, priv_readable, encrypted, unlocked, pub_b64, address, keyfile)
        loaded = essentials.keys_load_new(wallet_file)
        self.key = loaded[0]
        self.public_key_b64encoded = loaded[5]
        self.address = loaded[6]

    # --- raw protocol -----------------------------------------------------
    def command(self, command, options=None):
        s = socket.socket()
        s.settimeout(self.timeout)
        s.connect((self.ip, self.port))
        try:
            connections.send(s, command)
            for opt in (options or []):
                connections.send(s, opt)
            return connections.receive(s)
        finally:
            s.close()

    # --- convenience ------------------------------------------------------
    def mine(self, count=1):
        before = self.block_height()
        result = self.command("regtest_generate", [count])
        # regtest_generate can return before the block is committed to the queryable ledger; wait until
        # the chain has actually advanced so send->mine->read patterns are race-free (suite robustness).
        deadline = time.time() + 10
        while self.block_height() < before + count and time.time() < deadline:
            time.sleep(0.05)
        return result

    def block_height(self):
        return self.command("statusjson")["blocks"]

    def balance(self, address=None):
        return float(self.command("balanceget", [address or self.address])[0])

    def latest_transactions(self, num=1, address=None):
        # addlistlimjson returns the most recent transactions for an address (newest first)
        return self.command("addlistlimjson", [address or self.address, num])

    def send(self, recipient, amount, operation="", data=""):
        """Build, sign (in-tree polysign) and submit a transaction. Returns the txid (sig[:56])."""
        timestamp = "%.2f" % time.time()
        signed = essentials.sign_rsa(
            timestamp, self.address, recipient, amount, operation, data,
            self.key, self.public_key_b64encoded,
        )
        if not signed:
            raise RuntimeError("local signing failed")
        tx = list(signed)  # (timestamp, address, recipient, amount, signature, public_key, operation, openfield)
        self.command("mpinsert", [tx])
        return signed[4][:56]

    def clear_cache(self):
        # No client-side caching here; kept for parity with the old BismuthClient API.
        pass
