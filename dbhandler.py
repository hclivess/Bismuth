"""
Database handler module for Bismuth nodes
Needed for Json-RPC server or other third party interaction

modular handlers will need access to the database under some form, so it needs to be modular too.
Here, I just duplicated the minimum needed code from node, further refactoring with classes will follow.
"""

import time
import sqlite3



class DbHandler:
    def __init__(self, index_db, ledger_path_conf, hyper_path_conf, full_ledger, ram_conf, ledger_ram_file, logger):
        self.logger = logger

        self.index = sqlite3.connect(index_db, timeout=1, check_same_thread=False)
        self.index.text_factory = str
        self.index.execute("PRAGMA page_size = 4096;")
        self.index_cursor = self.index.cursor()

        self.hdd = sqlite3.connect(ledger_path_conf, timeout=1, check_same_thread=False)
        self.hdd.text_factory = str
        self.hdd.execute("PRAGMA page_size = 4096;")
        self.h = self.hdd.cursor()

        self.hdd2 = sqlite3.connect(hyper_path_conf, timeout=1, check_same_thread=False)
        self.hdd2.text_factory = str
        self.hdd2.execute("PRAGMA page_size = 4096;")
        self.h2 = self.hdd2.cursor()

        if full_ledger:
            self.h3 = self.h
        else:
            self.h3 = self.h2

        if ram_conf:  # select RAM as source database
            self.source_db = sqlite3.connect(ledger_ram_file, uri=True, timeout=1, check_same_thread=False)
        else:  # select hyper.db as source database
            self.source_db = sqlite3.connect(hyper_path_conf, timeout=1, check_same_thread=False)

        self.source_db.text_factory = str
        self.sc = self.source_db.cursor()

        try:
            if ram_conf:
                self.conn = sqlite3.connect(ledger_ram_file, uri=True, isolation_level=None, check_same_thread=False)
            else:
                self.conn = sqlite3.connect(hyper_path_conf, uri=True, isolation_level=None, check_same_thread=False)

            self.conn.execute('PRAGMA journal_mode = WAL;')
            self.conn.execute("PRAGMA page_size = 4096;")
            self.conn.text_factory = str
            self.c = self.conn.cursor()

        except Exception as e:
            logger.app_log.info(e)

    def cursor_define(self,cursor):
        if cursor == "c":
            cursor = self.c
        if cursor == "h":
            cursor = self.h
        if cursor == "h2":
            cursor = self.h2
        if cursor == "h3":
            cursor = self.h3
        if cursor == "index_cursor":
            cursor = self.index_cursor
        if cursor == "sc":
            cursor = self.sc
        return cursor

    def commit(self, cursor):
        cursor = self.cursor_define(cursor)

        """Secure commit for slow nodes"""
        while True:
            try:
                cursor.commit()
                break
            except Exception as e:
                self.logger.app_log.warning(f"Database cursor: {cursor}")
                self.logger.app_log.warning(f"Database retry reason: {e}")
                time.sleep(1)
        return self

    def execute(self, cursor, query):
        """Secure execute for slow nodes"""
        cursor = self.cursor_define(cursor)

        while True:
            try:
                cursor.execute(query)
                break
            except sqlite3.InterfaceError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {query}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except sqlite3.IntegrityError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {query}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except Exception as e:
                self.logger.app_log.warning(f"Database query: {cursor} {query}")
                self.logger.app_log.warning(f"Database retry reason: {e}")
                time.sleep(1)
        return self

    def execute_many(self, cursor, query, param):
        cursor = self.cursor_define(cursor)

        while True:
            try:
                cursor.executemany(query, param)
                break
            except sqlite3.InterfaceError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {query} {param}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except sqlite3.IntegrityError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {query}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except Exception as e:
                self.logger.app_log.warning(f"Database query: {cursor} {query} {param}")
                self.logger.app_log.warning(f"Database retry reason: {e}")
                time.sleep(0.1)
        return self

    def execute_param(self, cursor, query, param):
        """Secure execute w/ param for slow nodes"""
        cursor = self.cursor_define(cursor)

        while True:
            try:
                cursor.execute(query, param)
                break
            except sqlite3.InterfaceError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {query} {param}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except sqlite3.IntegrityError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {query}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except Exception as e:
                self.logger.app_log.warning(f"Database query: {cursor} {query} {param}")
                self.logger.app_log.warning(f"Database retry reason: {e}")
                time.sleep(1)
        return self

    def fetchall(self, cursor):
        cursor = self.cursor_define(cursor)

        return cursor.fetchall()

    def fetchone(self, cursor):
        cursor = self.cursor_define(cursor)

        return cursor.fetchone()

    def close(self):
        self.c.close()
        self.h.close()
        self.h2.close()
        self.h3.close()
        self.index_cursor.close()
        self.sc.close()