"""
Database handler module for Bismuth nodes - Optimized Version
"""

import time
import sqlite3
import essentials
from quantizer import quantize_two, quantize_eight, quantize_ten
import functools
from fork import Fork
import sys
import db_helpers
import balance_cache


def sql_trace_callback(log, id, statement):
    line = f"SQL[{id}] {statement}"
    log.warning(line)


class DbHandler:
    def __init__(self, index_db, ledger_path, hyper_path, ram, ledger_ram_file, logger, trace_db_calls=False):

        self.ram = ram
        self.ledger_ram_file = ledger_ram_file
        self.hyper_path = hyper_path
        self.logger = logger
        self.trace_db_calls = trace_db_calls
        self.index_db = index_db
        self.ledger_path = ledger_path

        # Initialize caches
        self._pubkey_cache = {}
        self._alias_cache = {}
        self._address_cache = {}
        self._max_cache = {}
        self._max_cache_time = 0

        self.index = sqlite3.connect(self.index_db, timeout=1)
        if self.trace_db_calls:
            self.index.set_trace_callback(functools.partial(sql_trace_callback,self.logger.app_log,"INDEX"))
        self.index.text_factory = str
        self.index.execute('PRAGMA case_sensitive_like = 1;')
        self.index_cursor = self.index.cursor()

        self.hdd = sqlite3.connect(self.ledger_path, timeout=1)
        if self.trace_db_calls:
            self.hdd.set_trace_callback(functools.partial(sql_trace_callback,self.logger.app_log,"HDD"))
        self.hdd.text_factory = str
        self.hdd.execute('PRAGMA case_sensitive_like = 1;')
        self.h = self.hdd.cursor()

        self.hdd2 = sqlite3.connect(self.hyper_path, timeout=1)
        if self.trace_db_calls:
            self.hdd2.set_trace_callback(functools.partial(sql_trace_callback,self.logger.app_log,"HDD2"))
        self.hdd2.text_factory = str
        self.hdd2.execute('PRAGMA case_sensitive_like = 1;')
        self.h2 = self.hdd2.cursor()

        if self.ram:
            self.conn = sqlite3.connect(self.ledger_ram_file, uri=True, isolation_level=None, timeout=1)
        else:
            self.conn = sqlite3.connect(self.hyper_path, uri=True, timeout=1)

        if self.trace_db_calls:
            self.conn.set_trace_callback(functools.partial(sql_trace_callback,self.logger.app_log,"CONN"))
        self.conn.execute('PRAGMA journal_mode = WAL;')
        self.conn.execute('PRAGMA case_sensitive_like = 1;')
        self.conn.text_factory = str
        self.c = self.conn.cursor()

        self.SQL_TO_TRANSACTIONS = "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        self.SQL_TO_MISC = "INSERT INTO misc VALUES (?,?)"

        # Apply performance optimizations to all connections
        self._optimize_connections()

    def _optimize_connections(self):
        """Apply SQLite performance optimizations to all connections"""
        for conn in [self.index, self.hdd, self.hdd2, self.conn]:
            try:
                conn.execute("PRAGMA synchronous = NORMAL")  # Faster than FULL
                conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
                conn.execute("PRAGMA temp_store = MEMORY")
                conn.execute("PRAGMA mmap_size = 536870912")  # 512MB memory-mapped I/O
            except Exception as e:
                self.logger.app_log.warning(f"Could not optimize connection: {e}")

    def ensure_indexes(self):
        """Create indexes for better performance - call this during setup/maintenance"""
        index_queries = [
            "CREATE INDEX IF NOT EXISTS idx_tx_block_height ON transactions(block_height)",
            "CREATE INDEX IF NOT EXISTS idx_tx_address ON transactions(address)",
            "CREATE INDEX IF NOT EXISTS idx_tx_recipient ON transactions(recipient)",
            "CREATE INDEX IF NOT EXISTS idx_tx_reward ON transactions(reward)",
            "CREATE INDEX IF NOT EXISTS idx_tx_block_hash ON transactions(block_hash)",
            "CREATE INDEX IF NOT EXISTS idx_misc_block_height ON misc(block_height)",
        ]

        for query in index_queries:
            try:
                self.h.execute(query)
                self.h2.execute(query)
                self.c.execute(query)
            except:
                pass  # Index might already exist

        try:
            self.index_cursor.execute("CREATE INDEX IF NOT EXISTS idx_aliases_address ON aliases(address)")
            self.index_cursor.execute("CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias)")
            self.index_cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_address ON tokens(address)")
            self.index_cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_recipient ON tokens(recipient)")
        except:
            pass

        self.commit(self.hdd)
        self.commit(self.hdd2)
        self.commit(self.conn)
        self.commit(self.index)

    def clear_caches(self):
        """Clear all internal caches - useful after rollbacks"""
        self._pubkey_cache.clear()
        self._alias_cache.clear()
        self._address_cache.clear()
        self._max_cache.clear()
        self._max_cache_time = 0

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
        while sys.getsizeof(
                str(blocks_fetched)) < 500000:  # limited size based on txs in blocks
            self.execute_param(self.h, (
                "SELECT timestamp,address,recipient,amount,signature,public_key,operation,openfield FROM transactions WHERE block_height > ? AND block_height <= ?;"),
                                              (str(int(block)), str(int(block + 1)),))
            result = self.h.fetchall()
            if not result:
                break
            blocks_fetched.extend([result])
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

    def backup_higher(self, block_height):
        "backup higher blocks than given, takes data from c, which normally means RAM"
        self.execute_param(self.c, "SELECT * FROM transactions WHERE block_height >= ?;", (block_height,))
        backup_data = self.c.fetchall()

        self.execute_param(self.c, "DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?", (block_height, -block_height))
        self.commit(self.conn)

        self.execute_param(self.c, "DELETE FROM misc WHERE block_height >= ?;", (block_height,))
        self.commit(self.conn)

        # Clear caches when data changes
        self.clear_caches()

        return backup_data

    def rollback_under(self, block_height):
        self.h.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?", (block_height, -block_height,))
        self.commit(self.hdd)

        self.h.execute("DELETE FROM misc WHERE block_height >= ?", (block_height,))
        self.commit(self.hdd)

        self.h2.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?", (block_height, -block_height,))
        self.commit(self.hdd2)

        self.h2.execute("DELETE FROM misc WHERE block_height >= ?", (block_height,))
        self.commit(self.hdd2)

        # Clear caches after rollback
        self.clear_caches()

    def rollback_to(self, block_height):
        # We don't need node to have the logger
        self.logger.app_log.error("rollback_to is deprecated, use rollback_under")
        self.rollback_under(block_height)

    def tokens_rollback(self, node, height):
        """Rollback Token index

        :param height: height index of token in chain

        Simply deletes from the `tokens` table where the block_height is
        greater than or equal to the :param height: and logs the new height

        returns None
        """
        try:
            self.execute_param(self.index_cursor, "DELETE FROM tokens WHERE block_height >= ?;", (height,))
            self.commit(self.index)

            node.logger.app_log.warning(f"Rolled back the token index below {(height)}")
        except Exception as e:
            node.logger.app_log.warning(f"Failed to roll back the token index below {(height)} due to {e}")

    def aliases_rollback(self, node, height):
        """Rollback Alias index

        :param height: height index of token in chain

        Simply deletes from the `aliases` table where the block_height is
        greater than or equal to the :param height: and logs the new height

        returns None
        """
        try:
            self.execute_param(self.index_cursor, "DELETE FROM aliases WHERE block_height >= ?;", (height,))
            self.commit(self.index)

            # Clear alias caches after rollback
            self._alias_cache.clear()
            self._address_cache.clear()

            node.logger.app_log.warning(f"Rolled back the alias index below {(height)}")
        except Exception as e:
            node.logger.app_log.warning(f"Failed to roll back the alias index below {(height)} due to {e}")

    def dev_reward(self,node,block_array,miner_tx,mining_reward,mirror_hash):
        self.execute_param(self.c, self.SQL_TO_TRANSACTIONS,
                                 (-block_array.block_height_new, str(miner_tx.q_block_timestamp), "Development Reward", str(node.genesis),
                                  str(mining_reward), "0", "0", mirror_hash, "0", "0", "0", "0"))
        self.commit(self.conn)

    def hn_reward(self,node,block_array,miner_tx,mirror_hash):
        fork = Fork()

        if node.is_testnet and node.last_block >= fork.POW_FORK_TESTNET:
            self.reward_sum = 24 - 10 * (node.last_block + 5 - fork.POW_FORK_TESTNET) / 3000000

        elif node.is_mainnet and node.last_block >= fork.POW_FORK:
            self.reward_sum = 24 - 10*(node.last_block + 5 - fork.POW_FORK)/3000000
        else:
            self.reward_sum = 24

        if self.reward_sum < 0.5:
            self.reward_sum = 0.5

        self.reward_sum = '{:.8f}'.format(self.reward_sum)

        self.execute_param(self.c, self.SQL_TO_TRANSACTIONS,
                           (-block_array.block_height_new, str(miner_tx.q_block_timestamp), "Hypernode Payouts",
                            "3e08b5538a4509d9daa99e01ca5912cda3e98a7f79ca01248c2bde16",
                            self.reward_sum, "0", "0", mirror_hash, "0", "0", "0", "0"))
        self.commit(self.conn)

    def to_db(self, block_array, diff_save, block_transactions):
        """Optimized version using batch operations"""
        self.execute_param(self.c, "INSERT INTO misc VALUES (?, ?)",
                                 (block_array.block_height_new, diff_save))

        # Prepare all transactions for batch insert
        prepared_transactions = []
        for transaction2 in block_transactions:
            prepared_transactions.append((
                str(transaction2[0]), str(transaction2[1]), str(transaction2[2]),
                str(transaction2[3]), str(transaction2[4]), str(transaction2[5]),
                str(transaction2[6]), str(transaction2[7]), str(transaction2[8]),
                str(transaction2[9]), str(transaction2[10]), str(transaction2[11])
            ))

        # Use executemany for batch insert - much faster
        if prepared_transactions:
            self.c.executemany(self.SQL_TO_TRANSACTIONS, prepared_transactions)

        # Single commit for all operations
        self.commit(self.conn)

    def db_to_drive(self, node):
        """Optimized version using batch operations"""
        try:
            if node.is_regnet:
                node.hdd_block = node.last_block
                node.hdd_hash = node.last_block_hash
                self.logger.app_log.warning(f"Chain: Regnet simulated move to HDD")
                return

            node.logger.app_log.warning(f"Chain: Moving new data to HDD, {node.hdd_block + 1} to {node.last_block} ")

            # Fetch all transactions
            self.execute_param(self.c,
                              "SELECT * FROM transactions "
                              "WHERE block_height > ? OR block_height < ? "
                              "ORDER BY block_height ASC",
                              (node.hdd_block, -node.hdd_block))
            result1 = self.c.fetchall()

            # Fetch all misc data
            self.execute_param(self.c,
                              "SELECT * FROM misc WHERE block_height > ? ORDER BY block_height ASC",
                              (node.hdd_block, ))
            result2 = self.c.fetchall()

            # Batch insert transactions
            if result1:
                self.h.executemany(self.SQL_TO_TRANSACTIONS, result1)
                self.commit(self.hdd)

                if node.ram:
                    self.h2.executemany(self.SQL_TO_TRANSACTIONS, result1)
                    self.commit(self.hdd2)

            # Batch insert misc
            if result2:
                self.h.executemany(self.SQL_TO_MISC, result2)
                self.commit(self.hdd)

                if node.ram:
                    self.h2.executemany(self.SQL_TO_MISC, result2)
                    self.commit(self.hdd2)

            node.hdd_block = node.last_block
            node.hdd_hash = node.last_block_hash

            node.logger.app_log.warning(f"Chain: {len(result1)} txs moved to HDD")
        except Exception as e:
            node.logger.app_log.warning(f"Chain: Exception Moving new data to HDD: {e}")

    # Aborts on InterfaceError/IntegrityError, retries other errors forever (sleep 1s).
    _ABORT_ON = (sqlite3.InterfaceError, sqlite3.IntegrityError)

    def commit(self, connection):
        """Secure commit for slow nodes"""
        db_helpers.retry_db(connection.commit, delay=1, log=self.logger.app_log, describe="commit")

    def execute(self, cursor, query):
        """Secure execute for slow nodes"""
        db_helpers.retry_db(lambda: cursor.execute(query), abort_on=self._ABORT_ON,
                            delay=1, log=self.logger.app_log, describe=str(query)[:100])

    def execute_param(self, cursor, query, param):
        """Secure execute w/ param for slow nodes"""
        db_helpers.retry_db(lambda: cursor.execute(query, param), abort_on=self._ABORT_ON,
                            delay=1, log=self.logger.app_log, describe=str(query)[:100])

    def fetchall(self, cursor, query, param=None):
        """Helper to simplify calling code, execute and fetch in a single line instead of 2"""
        if param is None:
            self.execute(cursor, query)
        else:
            self.execute_param(cursor, query, param)
        return cursor.fetchall()

    def fetchone(self, cursor, query, param=None):
        """Helper to simplify calling code, execute and fetch in a single line instead of 2"""
        if param is None:
            self.execute(cursor, query)
        else:
            self.execute_param(cursor, query, param)
        res = cursor.fetchone()
        if res:
            return res[0]
        return None

    def balance_get(self, address):
        """Authoritative ledger balance for an address, memoized per chain height (read-side accelerator)."""
        return balance_cache.get_balance(self, address)

    def close(self):
        self.index.close()
        self.hdd.close()
        self.hdd2.close()
        self.conn.close()