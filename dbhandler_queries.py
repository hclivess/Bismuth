"""
Read-only ledger/index queries for ``DbHandler``, split out as a mixin and recombined via
``class DbHandler(DbQueriesMixin, DbWriteMixin)``. Methods are unchanged and run on the composed
DbHandler instance (``self.h``/``self.c``/``self.index`` cursors come from ``DbHandler.__init__``).
"""
import time
import essentials
import sys
import balance_cache
from quantizer import quantize_two


class DbQueriesMixin:
    def last_block_hash(self):
        self.execute(self.c, "SELECT block_hash FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
        result = self.c.fetchone()[0]
        return result

    def pubkeyget(self, address):
        # Check cache first
        if address in self._pubkey_cache:
            return self._pubkey_cache[address]

        self.execute_param(self.c, "SELECT public_key FROM transactions WHERE address = ? and reward = 0 LIMIT 1", (address,))
        result = self.c.fetchone()[0]

        # Cache the result
        self._pubkey_cache[address] = result
        return result

    def addfromalias(self, alias):
        # Check cache first
        if alias in self._address_cache:
            return self._address_cache[alias]

        self.execute_param(self.index_cursor, "SELECT address FROM aliases WHERE alias = ? ORDER BY block_height ASC LIMIT 1;", (alias,))
        try:
            address_fetch = self.index_cursor.fetchone()[0]
        except:
            address_fetch = "No alias"

        # Cache the result
        self._address_cache[alias] = address_fetch
        return address_fetch

    def tokens_user(self, tokens_address):
        self.index_cursor.execute("SELECT DISTINCT token FROM tokens WHERE address = ? OR recipient = ?", (tokens_address, tokens_address))
        result = self.index_cursor.fetchall()
        return result

    def last_block_timestamp(self):
        self.execute(self.c, "SELECT timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
        return quantize_two(self.c.fetchone()[0])

    def difflast(self):
        self.execute(self.h, "SELECT block_height, difficulty FROM misc ORDER BY block_height DESC LIMIT 1")
        difflast = self.h.fetchone()
        return difflast

    def annverget(self, node):
        try:
            self.execute_param(self.h, "SELECT openfield FROM transactions WHERE address = ? AND operation = ? ORDER BY block_height DESC LIMIT 1", (node.genesis, "annver",))
            result = self.h.fetchone()[0]
        except:
            result = "?"
        return result

    def annget(self, node):
        try:
            self.execute_param(self.h, "SELECT openfield FROM transactions WHERE address = ? AND operation = ? ORDER BY block_height DESC LIMIT 1", (node.genesis, "ann",))
            result = self.h.fetchone()[0]
        except:
            result = "No announcement"
        return result

    def block_max_ram(self):
        self.execute(self.c, 'SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1')
        return essentials.format_raw_tx(self.c.fetchone())

    def aliasget(self, alias_address):
        # Check cache first
        if alias_address in self._alias_cache:
            return self._alias_cache[alias_address]

        self.execute_param(self.index_cursor, "SELECT alias FROM aliases WHERE address = ? ", (alias_address,))
        result = self.index_cursor.fetchall()
        if not result:
            result = [[alias_address]]

        # Cache the result
        self._alias_cache[alias_address] = result
        return result

    def aliasesget(self, aliases_request):
        results = []
        for alias_address in aliases_request:
            # Try cache first for each address
            if alias_address in self._alias_cache:
                cached = self._alias_cache[alias_address]
                if cached and cached != [[alias_address]]:
                    results.append(cached[0][0])
                    continue

            self.execute_param(self.index_cursor, (
                "SELECT alias FROM aliases WHERE address = ? ORDER BY block_height ASC LIMIT 1"), (alias_address,))
            try:
                result = self.index_cursor.fetchall()[0][0]
            except:
                result = alias_address
            results.append(result)
        return results

    def block_height_from_hash(self, data):
        try:
            self.execute_param(self.h, "SELECT block_height FROM transactions WHERE block_hash = ?;",(data,))
            result = self.h.fetchone()[0]
        except:
            result = None

        return result

    def blocksync(self, block):
        blocks_fetched = []
        # Track the accumulated serialized size incrementally. The previous condition re-stringified
        # the WHOLE accumulated list every iteration (O(n^2): serving one blocksget copied tens of MB
        # of throwaway strings), on the peer-sync serving hot path against the 23 GB ledger.
        total_size = 0
        while total_size < 500000:  # limited size based on txs in blocks
            self.execute_param(self.h, (
                "SELECT timestamp,address,recipient,amount,signature,public_key,operation,openfield FROM transactions WHERE block_height > ? AND block_height <= ?;"),
                                              (str(int(block)), str(int(block + 1)),))
            result = self.h.fetchall()
            if not result:
                break
            blocks_fetched.extend([result])
            total_size += sys.getsizeof(str(result))
            block = int(block) + 1
        return blocks_fetched

    def block_height_max(self):
        # Use caching with 1 second TTL
        current_time = time.time()
        if 'height_max' in self._max_cache and current_time - self._max_cache_time < 1:
            return self._max_cache['height_max']

        self.h.execute("SELECT max(block_height) FROM transactions")
        result = self.h.fetchone()[0]
        self._max_cache['height_max'] = result
        self._max_cache_time = current_time
        return result

    def block_height_max_diff(self):
        self.h.execute("SELECT max(block_height) FROM misc")
        return self.h.fetchone()[0]

    def block_height_max_hyper(self):
        self.h2.execute("SELECT max(block_height) FROM transactions")
        return self.h2.fetchone()[0]

    def block_height_max_diff_hyper(self):
        self.h2.execute("SELECT max(block_height) FROM misc")
        return self.h2.fetchone()[0]

    def balance_get(self, address):
        """Authoritative ledger balance for an address, memoized per chain height (read-side accelerator)."""
        return balance_cache.get_balance(self, address)
