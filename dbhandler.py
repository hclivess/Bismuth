"""
Database handler module for Bismuth nodes
"""

import time
import sqlite3
import queue

class DbHandler:
    def __init__(self, index_db, ledger_path_conf, hyper_path_conf, full_ledger, ram_conf, ledger_ram_file, logger, queue):
        self.logger = logger
        self.full_ledger = full_ledger

        self.index = sqlite3.connect(index_db, timeout=1)
        self.index.text_factory = str
        self.index.execute("PRAGMA page_size = 4096;")
        self.index_cursor = self.index.cursor()

        self.hdd = sqlite3.connect(ledger_path_conf, timeout=1)
        self.hdd.text_factory = str
        self.hdd.execute("PRAGMA page_size = 4096;")
        self.h = self.hdd.cursor()

        self.hdd2 = sqlite3.connect(hyper_path_conf, timeout=1)
        self.hdd2.text_factory = str
        self.hdd2.execute("PRAGMA page_size = 4096;")
        self.h2 = self.hdd2.cursor()

        if ram_conf:  # select RAM as source database
            self.source_db = sqlite3.connect(ledger_ram_file, uri=True, timeout=1)
        else:  # select hyper.db as source database
            self.source_db = sqlite3.connect(hyper_path_conf, timeout=1)
        self.source_db.text_factory = str
        self.sc = self.source_db.cursor()

        try:
            if ram_conf:
                self.conn = sqlite3.connect(ledger_ram_file, uri=True, isolation_level=None)
            else:
                self.conn = sqlite3.connect(hyper_path_conf, uri=True, isolation_level=None)

            self.conn.execute('PRAGMA journal_mode = WAL;')
            self.conn.execute("PRAGMA page_size = 4096;")
            self.conn.text_factory = str
            self.c = self.conn.cursor()

        except Exception as e:
            logger.app_log.info(e)

        if self.full_ledger:
            self.h3 = self.hdd.cursor()
        else:
            self.h3 = self.hdd2.cursor()

    def connection_define(self, connection):
        if connection == "conn":
            return self.conn
        if connection == "hdd":
            return self.hdd
        if connection == "hdd2":
            return self.hdd2
        if connection == "index":
            return self.index
        if connection == "source_db":
            return self.source_db

    def cursor_define(self,cursor):
        if cursor == "c":
            return self.c
        if cursor == "h":
            return self.h
        if cursor == "h2":
            return self.h2
        if cursor == "h3":
            return self.h3
        if cursor == "index_cursor":
            return self.index_cursor
        if cursor == "sc":
            return self.sc

    def commit(self, conn):
        connection = self.connection_define(conn)
        """Secure commit for slow nodes"""
        while True:
            try:
                connection.commit()
                break
            except Exception as e:
                self.logger.app_log.warning(f"Database connection: {connection}")
                self.logger.app_log.warning(f"Database retry reason: {e}")
                time.sleep(5)
        return self

    def execute(self, c, query):
        cursor = self.cursor_define(c)

        """Secure execute for slow nodes"""
        while True:
            try:
                cursor.execute(query)
                break
            except sqlite3.InterfaceError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {query[:100]}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except sqlite3.IntegrityError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {query[:100]}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except Exception as e:
                self.logger.app_log.warning(f"Database query: {cursor} {query[:100]}")
                self.logger.app_log.warning(f"Database retry reason: {e}")
                time.sleep(5)
                raise


        return self

    def execute_many(self, c, query, param):
        cursor = self.cursor_define(c)

        while True:
            try:
                cursor.executemany(query, param)
                break
            except sqlite3.InterfaceError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {str(query)[:100]} {param}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except sqlite3.IntegrityError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {str(query)[:100]}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except Exception as e:
                self.logger.app_log.warning(f"Database query: {cursor} {str(query)[:100]} {param}")
                self.logger.app_log.warning(f"Database retry reason: {e}")
                time.sleep(5)


        return self

    def execute_param(self, c, query, param):
        """Secure execute w/ param for slow nodes"""
        cursor = self.cursor_define(c)

        while True:
            try:
                cursor.execute(query, param)
                break
            except sqlite3.InterfaceError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {str(query)[:100]} {param}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except sqlite3.IntegrityError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {str(query)[:100]}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except Exception as e:
                self.logger.app_log.warning(f"Database query: {cursor} {str(query)[:100]} {param}")
                self.logger.app_log.warning(f"Database retry reason: {e}")
                time.sleep(5)



        return self

    def fetchall(self, c):
        cursor = self.cursor_define(c)
        result = cursor.fetchall()
        return result

    def fetchone(self, c):
        cursor = self.cursor_define(c)
        result = cursor.fetchone()
        return result

    def close_all(self):
            self.conn.close()
            self.hdd.close()
            self.hdd2.close()
            self.index.close()
            self.source_db.close()
