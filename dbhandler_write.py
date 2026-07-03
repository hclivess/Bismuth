"""
Ledger write & rollback operations for ``DbHandler`` (block commit, hyperblock drive flush, dev/hn
rewards, index rollbacks), split out as a mixin and recombined via
``class DbHandler(DbQueriesMixin, DbWriteMixin)``. Amount columns go through ``amounts`` at the
integer-units storage boundary. Method *behaviour* (the bytes written, the SQL, the commit count) is
unchanged from the originals; the only additions here are the durability/recovery DOCUMENTATION below.

CRASH-RECOVERY CONTRACT — ledger.db is the single source of truth for the on-disk tip
-------------------------------------------------------------------------------------
The node keeps the block data in up to three SQLite files with NO atomic cross-DB transaction, so the
ORDER in which they commit defines what a crash (OOM/SIGKILL/power loss) can leave behind. The order is:

  1. working store  (``self.conn`` / cursor ``self.c``) — hyper.db when ``ram=False`` (live default),
     or the in-RAM ledger when ``ram=True``. The full block (``to_db`` + the reward/vm mirror rows) is
     staged and COMMITTED here FIRST, per block, inside the digest.
  2. ledger.db      (``self.hdd`` / cursor ``self.h``) — the AUTHORITATIVE full on-disk ledger. The
     staged rows are flushed here in ``db_to_drive`` and COMMITTED LAST (synchronous=FULL).
        - ram=False: hyper.db already holds the block from step 1, so ``db_to_drive`` writes only
          ledger.db. Net per-block order: hyper.db first, ledger.db last.
        - ram=True:  ``db_to_drive`` writes ledger.db (hdd) THEN re-mirrors to hyper.db (hdd2).
  3. index.db       (``self.index``) — aliases/tokens projection, committed separately.

Because ledger.db commits LAST in the live (ram=False) path, it is the TRAILING store: if a block made
it into ledger.db, it is guaranteed to already be in hyper.db too. The runtime relies on exactly this —
``node.hdd_block`` is derived from ledger.db (``block_height_max`` -> ``self.h``), making ledger.db the
authority for "how far the chain is committed". A crash between commit #1 and commit #2 therefore leaves
ledger.db AT or BELOW the other stores, never above.

The recovery rule that follows from this ordering: on restart the rollup/index always reconcile DOWN to
ledger.db's committed tip (the startup reconcile in chain_ops.reconcile_ledger_hyper trims every store
to the common ``min`` tip, which in the live path is ledger.db). The trimmed-off blocks are simply
re-downloaded from peers. This module must preserve the "ledger.db commits LAST / is the floor"
ordering; reordering it (committing ledger.db before the working store) would let ledger.db run AHEAD on
a crash and break the single-source-of-truth contract above.
"""
import amounts
import db_helpers
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

        # SYMMETRY FIX — the source of the recurring ledger/hyper split: backup_higher is the ONLY primitive
        # that lowers the working tip on a live reorg (its sole caller is chain_ops.blocknf), and it deleted
        # ONLY from the working store (self.c = hyper.db when ram=False), leaving ledger.db (self.h) AHEAD.
        # That orphaned ledger excess then poisons the unbounded "already have it" guards (block_already_exists
        # / the replay check) and wedges sync at the same height across restarts (the ledger=N/marker=N-k,
        # hyper=N-k fingerprint). ledger.db is NOT consensus — ledger_balance3 reads self.c (hyper) — so
        # trimming ledger rows ABOVE the working tip can never change a balance/overspend decision; they are
        # non-canonical orphans. Mirror rollback_under's ledger arm so the two stores can never diverge.
        self.h.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?",
                       (block_height, -block_height))
        self.commit(self.hdd)
        self.h.execute("DELETE FROM misc WHERE block_height >= ?", (block_height,))
        self.commit(self.hdd)
        self._stamp_marker_best_effort(self.hdd, block_height - 1)
        self._stamp_marker_best_effort(self.hdd2, block_height - 1)

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

        # Keep the lockstep markers honest: this rollback drops everything from `block_height` up, so
        # the new committed tip is block_height-1. Without this the markers would read STALE-HIGH until
        # the next db_to_drive; the reconcile clamps by the real tips so it would still recover safely,
        # but maintaining "marker == committed tip" here keeps the invariant true everywhere.
        self._stamp_marker_best_effort(self.hdd, block_height - 1)
        self._stamp_marker_best_effort(self.hdd2, block_height - 1)

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
        # SEAM (doc/26 stage 2): when the LMDB side-index is enabled, roll it back instead of index.db.
        ti = getattr(node, "token_index", None)
        if ti is not None:
            try:
                ti.tokens_rollback(height)
                node.logger.app_log.warning(f"Rolled back the LMDB token side-index below {(height)}")
            except Exception as e:
                node.logger.app_log.warning(f"Failed to roll back the LMDB token side-index below {(height)} due to {e}")
            return
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
        # SEAM (doc/26 stage 2): when the LMDB side-index is enabled, roll it back instead of index.db.
        ti = getattr(node, "token_index", None)
        if ti is not None:
            try:
                ti.aliases_rollback(height)
                self._alias_cache.clear()
                self._address_cache.clear()
                node.logger.app_log.warning(f"Rolled back the LMDB alias side-index below {(height)}")
            except Exception as e:
                node.logger.app_log.warning(f"Failed to roll back the LMDB alias side-index below {(height)} due to {e}")
            return
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

    def vm_payout(self, block_height, timestamp, mirror_hash, recipient, amount_units):
        """Settle a VM custody payout (doc/19): a consensus-generated negative-height ledger row crediting
        `recipient` from the vm_custody sink (so the sink's ledger balance tracks the sum of contract
        balances). Locally generated by re-executing the block's vm: txs, and rolled back with the ledger
        on a reorg, exactly like the reward mirrors."""
        import vm_engine
        amt = str(int(amount_units) if amounts.LEDGER_INTEGER else amounts.to_decimal(amount_units))
        self.execute_param(self.c, self.SQL_TO_TRANSACTIONS,
                           (-int(block_height), str(timestamp), vm_engine.VM_SINK, str(recipient),
                            amt, "0", "0", str(mirror_hash), "0", "0", "vm:payout", "0"))
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

        # Commit #1 of the per-block order (see the CRASH-RECOVERY CONTRACT in this module's docstring):
        # the working store (hyper.db when ram=False, else the RAM ledger) is committed FIRST, BEFORE
        # db_to_drive flushes to the authoritative ledger.db. ledger.db must stay the trailing/floor tip.
        self.commit(self.conn)

    def db_to_drive(self, node):
        """Optimized version using batch operations.

        Commit #2 of the per-block order: flush the staged block(s) from the working store to ledger.db
        (``self.hdd``), the AUTHORITATIVE on-disk tip, and commit it LAST. See this module's docstring
        for the full crash-recovery contract — on a crash between to_db's commit and this one, ledger.db
        ends up at-or-below the working/hyper tip, and the startup reconcile trims everything DOWN to it.

        LOCKSTEP (added): when ledger.db and hyper.db are distinct files (the live full-ledger path) we
        do this flush through the dedicated ATTACH commit connection so the ledger rows AND a
        ``commit_marker`` of ``node.last_block`` in BOTH files are written in ONE transaction. hyper.db
        already holds these block ROWS (to_db committed them per-block); here we only advance hyper.db's
        MARKER, so the marker means "the height at which both files agree" and it advances in step with
        ledger.db. A crash can leave the two markers at most ONE in-flight block apart (WAL is not
        cross-file atomic), and the startup reconcile heals down to the lower marker — provably bounded,
        never a persistent multi-block split. If the commit connection is unavailable we fall back to
        the exact legacy per-file commit (with the reconcile as the backstop, as before).
        """
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

            # Same rows, same bytes as before — only HOW they commit changes (atomic dual-marker when
            # the lockstep connection is live, else the original per-file commit).
            if self._separate_ledger_hyper and self.commit_conn is not None:
                self._db_to_drive_lockstep(node, result1, result2)
            else:
                self._db_to_drive_legacy(node, result1, result2)

            node.hdd_block = node.last_block
            node.hdd_hash = node.last_block_hash

            node.logger.app_log.warning(f"Chain: {len(result1)} txs moved to HDD")
        except Exception as e:
            node.logger.app_log.warning(f"Chain: Exception Moving new data to HDD: {e}")
            # Roll back any partial implicit transaction on the HDD connections. In legacy isolation
            # mode a failed executemany leaves rows pending in the still-open transaction; since
            # node.hdd_block is NOT advanced, the next flush re-fetches and re-inserts the SAME rows,
            # committing duplicates. A best-effort rollback discards the partial write so the retry is clean.
            for _conn in (self.hdd, self.hdd2):
                try:
                    if _conn is not None:
                        _conn.rollback()
                except Exception:
                    pass

    def _db_to_drive_lockstep(self, node, result1, result2):
        """Atomic dual-marker flush for the live full-ledger path (ledger.db + ATTACHed hyper.db).

        ONE transaction on self.commit_conn writes the ledger.db block rows AND stamps
        ``committed_height = node.last_block`` into BOTH files' commit_marker. hyper.db's block rows are
        NOT rewritten here (to_db already committed them); only its marker advances, so the two markers
        move together and a crash bounds their divergence to a single in-flight block. ledger.db's own
        transactions/misc + marker are a single-file atomic unit either way, so ledger.db can never be
        seen with rows but a stale marker (or vice-versa)."""
        cc = self.commit_conn

        def _do():
            cc.execute("BEGIN IMMEDIATE")
            try:
                if result1:
                    cc.executemany(self.SQL_TO_TRANSACTIONS, result1)
                if result2:
                    cc.executemany(self.SQL_TO_MISC, result2)
                # Advance both markers to the height now durably on disk. `main` is ledger.db, `hyperdb`
                # is the ATTACHed hyper.db. Both UPDATEs ride inside this one BEGIN/COMMIT.
                cc.execute("UPDATE main.commit_marker SET committed_height = ? WHERE id = 0",
                           (node.last_block,))
                cc.execute("UPDATE hyperdb.commit_marker SET committed_height = ? WHERE id = 0",
                           (node.last_block,))
                cc.commit()
            except Exception:
                cc.rollback()
                raise

        # Same retry/durability discipline as self.commit (slow-node tolerant). An aborted attempt has
        # already rolled back inside _do, so a retry re-runs the whole atomic unit cleanly. Bound the
        # retries: _do runs schema-dependent marker UPDATEs, which fail DETERMINISTICALLY if a
        # commit_marker table is missing (creation is tolerated as non-fatal at open). Retrying forever
        # there wedges the node permanently while holding node.db_lock; instead fall back to the
        # per-file legacy flush, which stamps the marker best-effort and cannot get stuck on it.
        db_helpers.retry_db(_do, delay=1, max_tries=5, log=self.logger.app_log,
                            describe="lockstep db_to_drive",
                            on_give_up=lambda: self._db_to_drive_legacy(node, result1, result2))

        # Keep self.hdd / self.hdd2 (the readers' connections) seeing the just-committed state. Under WAL
        # a fresh statement on those connections already reads the latest commit, but ending any stale
        # implicit read txn is cheap insurance so a long-lived reader doesn't pin an old snapshot. We do
        # NOT clear_caches here — the original db_to_drive didn't, and node.hdd_block is set from
        # node.last_block directly (not via the 1s height cache), so the floor is unaffected.
        try:
            self.hdd.rollback()
            self.hdd2.rollback()
        except Exception:
            pass

    def _db_to_drive_legacy(self, node, result1, result2):
        """The original (pre-lockstep) per-file commit, kept verbatim as the fallback for: the
        ram=True path, the single-file (regnet/hyperblock) path, and any startup where the ATTACH
        commit connection could not be opened. The startup reconcile remains the backstop here.

        ram=True additionally re-mirrors to hyper.db (self.hdd2); we also stamp the markers per-file so
        recovery still has the unambiguous signal (best-effort — never blocks the flush)."""
        # Batch insert transactions. ledger.db (self.hdd) is committed here; in ram=False hyper.db
        # already has this block from to_db, so only ledger.db is written and it is the LAST commit for
        # the block. In ram=True the working store is the RAM ledger, so hyper.db (self.hdd2) is
        # re-mirrored from ledger.db right after — keep ledger.db FIRST so it is never behind hyper.db.
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

        # Best-effort marker stamping so reconcile still gets the explicit floor in these paths too.
        self._stamp_marker_best_effort(self.hdd, node.last_block)
        if node.ram:
            self._stamp_marker_best_effort(self.hdd2, node.last_block)

    def _stamp_marker_best_effort(self, conn, height):
        """Set commit_marker.committed_height on an already-open connection, swallowing any error (the
        table may not exist on a legacy DB). Never raises into the commit path."""
        try:
            conn.execute("UPDATE commit_marker SET committed_height = ? WHERE id = 0", (height,))
            conn.commit()
        except Exception:
            pass
