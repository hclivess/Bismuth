"""
Database handler module for Bismuth nodes - Optimized Version

``DbHandler`` is the single owner of the SQLite connections and queries. The read queries and the
write/rollback operations live in ``dbhandler_queries`` / ``dbhandler_write`` mixins and are recombined
below; this module keeps the connection lifecycle, the low-level SQL plumbing, and the canonical
``sql_trace_callback`` (imported by other modules from here).
"""
import sqlite3
import functools
import db_helpers
from dbhandler_queries import DbQueriesMixin
from dbhandler_write import DbWriteMixin


def sql_trace_callback(log, id, statement):
    line = f"SQL[{id}] {statement}"
    log.warning(line)


class DbHandler(DbQueriesMixin, DbWriteMixin):
    _ABORT_ON = (sqlite3.InterfaceError, sqlite3.IntegrityError)

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
            # token-first composite indexes: the balance/holders queries filter by token THEN address/
            # recipient (tokensv2 + /api/token), so a (token, …) index avoids scanning every token's rows
            self.index_cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_token_recipient ON tokens(token, recipient)")
            self.index_cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_token_address ON tokens(token, address)")
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

    def close(self):
        self.index.close()
        self.hdd.close()
        self.hdd2.close()
        self.conn.close()
