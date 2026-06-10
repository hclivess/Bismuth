"""
Dependency-light Bismuth test client.

Replaces the external `bismuthclient` for the test-suite: it speaks the node wire protocol with
the in-tree ``connections`` module and signs transactions with the in-tree ``polysign`` (via
``essentials.sign_rsa``). No bismuthclient / bismuthcore / tornado / ed25519 required, so the suite
runs on any modern Python where the node itself runs.
"""
import json
import socket
import time
import urllib.request

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
    def command(self, command, options=None, _tries=3):
        """Send a node command, retrying transient connection drops. A busy node (e.g. mid-digest of a heavy
        block such as a RingCT spend) can close a fresh socket -> 'Socket EOF'; that is infrastructure
        flakiness, not a command failure, so retry with backoff. A genuinely failing command fails all
        tries and re-raises."""
        last = None
        for attempt in range(_tries):
            s = socket.socket()
            s.settimeout(self.timeout)
            try:
                s.connect((self.ip, self.port))
                connections.send(s, command)
                for opt in (options or []):
                    connections.send(s, opt)
                return connections.receive(s)
            except (RuntimeError, OSError) as e:
                last = e
                time.sleep(0.5 * (attempt + 1))
            finally:
                s.close()
        raise last

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

    def mine_real(self, count=1):
        """REGNET: drive the ACTUAL solo miner (miner.py via regtest_mine) — the mainnet code path —
        rather than the regnet-only generator. Real Heavy3, so allow more time."""
        before = self.block_height()
        result = self.command("regtest_mine", [count])
        deadline = time.time() + 60
        while self.block_height() < before + count and time.time() < deadline:
            time.sleep(0.1)
        return result

    def rollback(self, below_height):
        """REGNET test helper: roll the chain back below `below_height` (drives chain_ops.rollback —
        the ledger AND every integrated auxiliary store, in sync). Returns the new tip."""
        self.command("regtest_rollback", [below_height])
        deadline = time.time() + 10
        while self.block_height() != below_height - 1 and time.time() < deadline:
            time.sleep(0.05)
        return self.block_height()

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

    def send_with_signer(self, recipient, amount, signer, operation="", data=""):
        """Build, sign with an arbitrary polysign ECDSA/ED25519 signer (e.g. an HD-derived key), and
        submit. The sender address is the signer's own address. Returns the txid."""
        import hd_wallet
        timestamp = "%.2f" % time.time()
        tx = hd_wallet.sign_transaction(signer, timestamp, signer.address(), recipient,
                                        amount, operation, data)
        self.command("mpinsert", [list(tx)])
        return tx[4][:56]

    def send_raw_tx(self, tx):
        """Submit a fully pre-built + signed 8-field tx tuple (e.g. a native multisig spend assembled by
        multisig_wallet.MultisigAccount). Returns the txid (sig[:56])."""
        self.command("mpinsert", [list(tx)])
        return tx[4][:56]

    # --- API submission (the post-hardfork write path; no socket) ----------
    def api_submit(self, tx, api_port=3031):
        """Submit a signed 8-field tx over the REST API (POST /api/transaction) — the post-hardfork
        transport that replaces the socket mpinsert. Returns the parsed JSON response."""
        url = "http://%s:%d/api/transaction" % (self.ip, int(api_port))
        body = json.dumps({"transaction": list(tx)}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def send_via_api(self, recipient, amount, operation="", data="", api_port=3031):
        """Sign a tx with the RSA wallet and submit it over the REST API (not the socket). Returns txid."""
        timestamp = "%.2f" % time.time()
        signed = essentials.sign_rsa(timestamp, self.address, recipient, amount, operation, data,
                                     self.key, self.public_key_b64encoded)
        if not signed:
            raise RuntimeError("local signing failed")
        self.api_submit(list(signed), api_port=api_port)
        return signed[4][:56]

    def clear_cache(self):
        # No client-side caching here; kept for parity with the old BismuthClient API.
        pass
