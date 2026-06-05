"""
Ledger write & rollback operations for ``DbHandler`` (block commit, hyperblock drive flush, dev/hn
rewards, index rollbacks), split out as a mixin and recombined via
``class DbHandler(DbQueriesMixin, DbWriteMixin)``. Amount columns go through ``amounts`` at the
integer-units storage boundary. Methods are byte-identical to the originals.
"""
import amounts
from fork import Fork


class DbWriteMixin:
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
        dev_amount = str(amounts.to_units(mining_reward) if amounts.LEDGER_INTEGER else mining_reward)
        self.execute_param(self.c, self.SQL_TO_TRANSACTIONS,
                                 (-block_array.block_height_new, str(miner_tx.q_block_timestamp), "Development Reward", str(node.genesis),
                                  dev_amount, "0", "0", mirror_hash, "0", "0", "0", "0"))
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

        hn_amount = str(amounts.to_units(self.reward_sum) if amounts.LEDGER_INTEGER else self.reward_sum)
        self.execute_param(self.c, self.SQL_TO_TRANSACTIONS,
                           (-block_array.block_height_new, str(miner_tx.q_block_timestamp), "Hypernode Payouts",
                            "3e08b5538a4509d9daa99e01ca5912cda3e98a7f79ca01248c2bde16",
                            hn_amount, "0", "0", mirror_hash, "0", "0", "0", "0"))
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
