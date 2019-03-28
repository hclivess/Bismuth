"""
Database handler module for Bismuth nodes
"""

import time
import sqlite3
import essentials

class DbHandler:
    def __init__(self, index_db, ledger_path, hyper_path, ram, ledger_ram_file, logger):
        self.ram = ram
        self.ledger_ram_file = ledger_ram_file
        self.hyper_path = hyper_path

        self.logger = logger

        self.index = sqlite3.connect(index_db, timeout=1)
        self.index.text_factory = str
        self.index_cursor = self.index.cursor()

        self.hdd = sqlite3.connect(ledger_path, timeout=1)
        self.hdd.text_factory = str
        self.h = self.hdd.cursor()

        self.hdd2 = sqlite3.connect(hyper_path, timeout=1)
        self.hdd2.text_factory = str
        self.h2 = self.hdd2.cursor()


        if self.ram:
            self.conn = sqlite3.connect(self.ledger_ram_file, uri=True, isolation_level=None, timeout=1)
        else:
            self.conn = sqlite3.connect(self.hyper_path, uri=True, timeout=1)

        self.conn.execute('PRAGMA journal_mode = WAL;')
        self.conn.text_factory = str
        self.c = self.conn.cursor()

    def block_max_ram(self):
        self.execute(self.c, 'SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1')
        return essentials.format_raw_tx(self.c.fetchone())


    def block_height_max(self):
        self.h.execute("SELECT max(block_height) FROM transactions")
        return self.h.fetchone()[0]

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

        return backup_data



    def rollback_to(self, block_height):
        self.h.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?", (block_height, -block_height,))
        self.commit(self.hdd)

        self.h.execute("DELETE FROM misc WHERE block_height >= ?", (block_height,))
        self.commit(self.hdd)

        self.h2.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?", (block_height, -block_height,))
        self.commit(self.hdd2)

        self.h2.execute("DELETE FROM misc WHERE block_height >= ?", (block_height,))
        self.commit(self.hdd2)

    def tokens_rollback(self, node, height, db_handler):
        """Rollback Token index

        :param height: height index of token in chain

        Simply deletes from the `tokens` table where the block_height is
        greater than or equal to the :param height: and logs the new height

        returns None
        """
        try:
            self.execute_param(db_handler.index_cursor, "DELETE FROM tokens WHERE block_height >= ?;", (height,))
            self.commit(db_handler.index)

            node.logger.app_log.warning(f"Rolled back the token index below {(height)}")
        except Exception as e:
            node.logger.app_log.warning(f"Failed to roll back the token index below {(height)} due to {e}")

    def staking_rollback(self, node, height):
        """Rollback staking index

        :param height: height index of token in chain

        Simply deletes from the `staking` table where the block_height is
        greater than or equal to the :param height: and logs the new height

        returns None
        """
        try:
            self.execute_param(self.index_cursor, "DELETE FROM staking WHERE block_height >= ?;", (height,))
            self.commit(self.index)

            node.logger.app_log.warning(f"Rolled back the staking index below {(height)}")
        except Exception as e:
            node.logger.app_log.warning(f"Failed to roll back the staking index below {(height)} due to {e}")

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

            node.logger.app_log.warning(f"Rolled back the alias index below {(height)}")
        except Exception as e:
            node.logger.app_log.warning(f"Failed to roll back the alias index below {(height)} due to {e}")

    def dev_reward(self,node,block_array,miner_tx,mining_reward,mirror_hash):
        self.execute_param(self.c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (-block_array.block_height_new, str(miner_tx.q_block_timestamp), "Development Reward", str(node.genesis),
                                  str(mining_reward), "0", "0", mirror_hash, "0", "0", "0", "0"))
        self.commit(self.conn)

        self.execute_param(self.c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (-block_array.block_height_new, str(miner_tx.q_block_timestamp), "Hypernode Payouts",
                                  "3e08b5538a4509d9daa99e01ca5912cda3e98a7f79ca01248c2bde16",
                                  "8", "0", "0", mirror_hash, "0", "0", "0", "0"))
        self.commit(self.conn)


    def commit(self, connection):
        """Secure commit for slow nodes"""
        while True:
            try:
                connection.commit()
                break
            except Exception as e:
                self.logger.app_log.warning(f"Database connection: {connection}")
                self.logger.app_log.warning(f"Database retry reason: {e}")
                time.sleep(0.1)

    def execute(self, cursor, query):
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
                time.sleep(0.1)

    """
    def execute_many(self, cursor, query, param):

        while True:
            try:
                cursor.executemany(query, param)
                break
            except sqlite3.InterfaceError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {str(query)[:100]} {str(param)[:100]}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except sqlite3.IntegrityError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {str(query)[:100]}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except Exception as e:
                self.logger.app_log.warning(f"Database query: {cursor} {str(query)[:100]} {str(param)[:100]}")
                self.logger.app_log.warning(f"Database retry reason: {e}")
                time.sleep(0.1)
    """
    def execute_param(self, cursor, query, param):
        """Secure execute w/ param for slow nodes"""

        while True:
            try:
                cursor.execute(query, param)
                break
            except sqlite3.InterfaceError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {str(query)[:100]} {str(param)[:100]}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except sqlite3.IntegrityError as e:
                self.logger.app_log.warning(f"Database query to abort: {cursor} {str(query)[:100]}")
                self.logger.app_log.warning(f"Database abortion reason: {e}")
                break
            except Exception as e:
                self.logger.app_log.warning(f"Database query: {cursor} {str(query)[:100]} {str(param)[:100]}")
                self.logger.app_log.warning(f"Database retry reason: {e}")
                time.sleep(0.1)

    def close_all(self):
        self.index.close()
        self.hdd.close()
        self.hdd2.close()
        self.index.close()
