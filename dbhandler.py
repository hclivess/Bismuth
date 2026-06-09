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
        self.conn.execute('PRAGMA case_sensitive_like = 1;')
        self.conn.text_factory = str
        self.c = self.conn.cursor()

        self.SQL_TO_TRANSACTIONS = "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        self.SQL_TO_MISC = "INSERT INTO misc VALUES (?,?)"

        # Apply crash-safety + performance pragmas to every connection (WAL, synchronous, …). This is
        # the SINGLE place journal_mode/synchronous are set, so no store the node opens can be left
        # without them (the per-connection WAL on self.conn above used to be the only one).
        self._optimize_connections()

    def _set_crash_safe_pragmas(self, conn, name, synchronous):
        """Make one SQLite connection crash-safe.

        WAL + a non-OFF `synchronous` is what gives us "a SIGKILL/OOM mid-write rolls back cleanly to
        the last COMMITTED transaction instead of leaving a torn/half-written page". WAL also lets a
        reader (the API, the startup reconcile) see the last committed state while a writer is mid txn.

        `synchronous`:
          * FULL   for the authoritative ledger (ledger.db / self.hdd) — fsync the WAL on every commit
                   so an acknowledged block survives a power cut, not just a process kill.
          * NORMAL elsewhere (index / hyper / working) — fsync only at checkpoint; a crash can still
                   only lose whole un-checkpointed transactions, never tear one, and these stores are
                   re-derivable / reconciled DOWN to ledger.db on restart anyway.

        We read journal_mode BACK and log loudly if it did not stick (e.g. an OS that refuses WAL on a
        network filesystem silently falls back to the old rollback journal), because the whole
        durability contract depends on it actually being WAL. (An in-memory store — the ram=True working
        ledger — legitimately reports 'memory' and cannot use WAL; that is expected, not a failure.)

        This changes ONLY durability/journaling — never which bytes get written.
        """
        try:
            row = conn.execute("PRAGMA journal_mode = WAL;").fetchone()
            mode = (row[0] if row else "").lower()
            if mode not in ("wal", "memory"):
                self.logger.app_log.warning(
                    f"DB durability: {name} did NOT enter WAL mode (got '{mode}'); crash-safety is "
                    f"degraded — an unclean kill may leave this store torn.")
            conn.execute(f"PRAGMA synchronous = {synchronous};")
        except Exception as e:
            self.logger.app_log.warning(f"Could not set crash-safe pragmas on {name}: {e}")

    def _optimize_connections(self):
        """Apply crash-safety (WAL + synchronous) and performance pragmas to all connections.

        Crash-safety is applied to EVERY store the node opens (index, ledger, hyper, working) so an
        OOM/SIGKILL mid-write rolls the killed transaction back rather than corrupting the file. The
        ledger (self.hdd / ledger.db) is the authoritative tip, so it gets synchronous=FULL; the others
        get NORMAL (still tear-proof under WAL, just weaker against a power cut, and they reconcile DOWN
        to the ledger on restart).
        """
        # (connection, log-name, synchronous level). self.conn aliases hyper.db (ram=False) or the RAM
        # ledger (ram=True), so it shares the working/hyper durability level.
        for conn, name, synchronous in (
            (self.index, "index.db", "NORMAL"),
            (self.hdd, "ledger.db", "FULL"),
            (self.hdd2, "hyper.db", "NORMAL"),
            (self.conn, "working", "NORMAL"),
        ):
            self._set_crash_safe_pragmas(conn, name, synchronous)

        for conn in [self.index, self.hdd, self.hdd2, self.conn]:
            try:
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
