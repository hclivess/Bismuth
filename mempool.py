"""
Mempool module for Bismuth nodes
"""

import sqlite3
import threading
import time
import sys
import os
import base64
from decimal import *
from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
import hashlib
import json

from quantizer import *
import essentials

__version__ = "0.0.4"


MEMPOOL = None

# remove after hf
drift_limit = 30

# If set to true, will always send empty Tx to other peers (but will accept theirs)
# Only to be used for debug/testing purposes
DEBUG_DO_NOT_SEND_TX = False

"""
Common Sql requests
"""

# Create mempool table
SQL_CREATE = "CREATE TABLE IF NOT EXISTS transactions (" \
             "timestamp TEXT, address TEXT, recipient TEXT, amount TEXT, signature TEXT, " \
             "public_key TEXT, operation TEXT, openfield TEXT)"

# Purge old txs that may be stuck
SQL_PURGE = "DELETE FROM transactions WHERE timestamp <= strftime('%s', 'now', '-1 day')"

# Delete all transactions
SQL_CLEAR = "DELETE FROM transactions"

# Check for presence of a given tx signature
SQL_SIG_CHECK = 'SELECT * FROM transactions WHERE signature = ?'

# delete a single tx
SQL_DELETE_TX = 'DELETE FROM transactions WHERE signature = ?'

# Selects all tx from mempool
SQL_SELECT_ALL_TXS = 'SELECT * FROM transactions'

# Counts distinct senders from mempool
SQL_COUNT_DISTINCT_SENDERS = 'SELECT COUNT(DISTINCT(address)) FROM transactions'

# Counts distinct recipients from mempool
SQL_COUNT_DISTINCT_RECIPIENTS = 'SELECT COUNT(DISTINCT(recipient)) FROM transactions'

# A single requets for status info
SQL_STATUS = 'SELECT COUNT(*) AS nb, SUM(LENGTH(openfield)) AS len, COUNT(DISTINCT(address)) as senders, COUNT(DISTINCT(recipient)) as recipients FROM transactions'

# Select Tx to be sent to a peer
SQL_SELECT_TX_TO_SEND = 'SELECT * FROM transactions ORDER BY amount DESC'

# Select Tx to be sent to a peer since the given ts
SQL_SELECT_TX_TO_SEND_SINCE = 'SELECT * FROM transactions where timestamp > ? ORDER BY amount DESC'


class Mempool:
    """The mempool manager. Thread safe"""

    def __init__(self, app_log, config=None, db_lock=None):
        try:
            self.app_log = app_log
            self.config = config
            self.db_lock = db_lock
            self.ram = self.config.mempool_ram_conf
            self.lock = threading.Lock()
            self.peers_lock = threading.Lock()
            # ip: last time sent
            self.peers_sent= dict()
            self.db = None
            self.cursor = None
            self.check()
        except Exception as e:
            self.app_log.error("Error creating mempool: {}".format(e))

    def check(self):
        """
        Checks if mempool exists, create if not.
        :return:
        """
        self.app_log.info("Mempool Check")
        with self.lock:
            if self.ram:
                self.db = sqlite3.connect('file:mempool?mode=memory&cache=shared',
                                          uri=True, timeout=1, isolation_level=None,
                                          check_same_thread=False
                                          )
                self.db.execute('PRAGMA journal_mode = WAL;')
                self.db.execute("PRAGMA page_size = 4096;")
                self.db.text_factory = str
                self.cursor = self.db.cursor()
                self.cursor.execute(SQL_CREATE)
                self.db.commit()
                self.app_log.info("Status: In memory mempool file created")
            else:
                self.db = sqlite3.connect('mempool.db', timeout=1,
                                          check_same_thread=False
                                          )
                self.db.text_factory = str
                self.cursor = self.db.cursor()

                # check if mempool needs recreating
                self.cursor.execute("PRAGMA table_info('transactions')")
                if len(self.cursor.fetchall()) != 8:
                    self.db.close()
                    os.remove("mempool.db")
                    self.db = sqlite3.connect('mempool.db', timeout=1,
                                              check_same_thread=False
                                              )
                    self.db.text_factory = str
                    self.cursor = self.db.cursor()
                    self.execute(SQL_CREATE)
                    self.commit()
                    self.app_log.info("Status: Recreated mempool file")

    # check if mempool needs recreating

    def execute(self, sql, param=None, cursor=None):
        """
        Safely execute the request
        :param sql:
        :param param:
        :param cursor: optional. will use the locked shared cursor if None
        :return:
        """
        # TODO: add a try count and die if we lock
        while True:
            try:
                if not cursor:
                    cursor = self.cursor
                if param:
                    cursor.execute(sql, param)
                else:
                    cursor.execute(sql)
                break
            except Exception as e:
                self.app_log.warning("Database query: {} {}".format(cursor, sql))
                self.app_log.warning("Database retry reason: {}".format(e))
                time.sleep(0.1)

    def commit(self):
        """
        Safe commit
        :return:
        """
        # no lock on execute and commit. locks are on full atomic operations only
        while True:
            try:
                self.db.commit()
                break
            except Exception as e:
                self.app_log.warning("Database retry reason: {}".format(e))
                time.sleep(0.1)

    def fetchone(self, sql, param=None, write=False):
        """
        Fetchs one and Returns data
        :param sql:
        :param param:
        :param write: if the requests involves write, set to True to request a Lock
        :return:
        """
        if write:
            with self.lock:
                self.execute(sql, param)
                return self.cursor.fetchone()
        else:
            cursor = self.db.cursor()
            self.execute(sql, param, cursor)
            return cursor.fetchone()

    def fetchall(self, sql, param=None, write=False):
        """
        Fetchs all and Returns data
        :param sql:
        :param param:
        :param write: if the requests involves write, set to True to request a Lock
        :return:
        """
        if write:
            with self.lock:
                self.execute(sql, param)
                return self.cursor.fetchall()
        else:
            cursor = self.db.cursor()
            self.execute(sql, param, cursor)
            return cursor.fetchall()

    def vacuum(self):
        """
        Maintenance
        :return:
        """
        with self.lock:
            self.execute("VACUUM")

    def close(self):
        if self.db:
            self.db.close()

    def purge(self):
        """
        Purge old txs
        :return:
        """
        while self.lock.locked():
            time.sleep(0.5)
        with self.lock:
            self.execute(SQL_PURGE)
            self.commit()

    def clear(self):
        """
        Empty mempool
        :return:
        """
        with self.lock:
            self.execute(SQL_CLEAR)
            self.commit()

    def delete_transaction(self, signature):
        """
        Delete a single tx by its id
        :return:
        """
        with self.lock:
            self.execute(SQL_DELETE_TX, (signature,))
            self.commit()

    def sig_check(self, signature):
        """
        Returns presence of the sig in the mempool
        :param signature:
        :return: Boolean
        """
        try:
            self.fetchall(SQL_SIG_CHECK, (signature,))
            return True
        except:
            return False

    def status(self):
        """
        Stats on the current mempool
        :return: tuple(tx#, openfield len, distinct sender#, distinct recipients#
        """
        try:
            limit = time.time()
            frozen = [peer for peer in self.peers_sent if self.peers_sent[peer] > limit ]
            self.app_log.warning("Status: MEMPOOL Frozen = {}".format(", ".join(frozen)))
            # print(limit, self.peers_sent, frozen)
            # Cleanup old nodes not synced since 15 min
            limit = limit - 15 * 60
            with self.peers_lock:
                self.peers_sent = {peer:self.peers_sent[peer] for peer in self.peers_sent if self.peers_sent[peer] > limit }
            self.app_log.warning("Status: MEMPOOL Live = {}".format(", ".join(set(self.peers_sent.keys()) - set(frozen))))
            status = self.fetchall(SQL_STATUS)
            count, open_len, senders, recipients = status[0]
            #self.app_log.warning("Status: MEMPOOL {}".format(json.dumps(status[0])))
            self.app_log.warning("Status: MEMPOOL {} Txs from {} senders to {} distinct recipients. Openfield len {}".format(count, senders, recipients, open_len ))
            return status[0]
        except:
            return 0

    def size(self):
        """
        Curent size of the mempool in Mo
        :return:
        """
        try:
            mempool_txs = self.fetchall(SQL_SELECT_ALL_TXS)
            mempool_size = sys.getsizeof(str(mempool_txs)) / 1000000.0
            return mempool_size
        except:
            return 0

    def sent(self, peer_ip):
        """
        record time of last mempool send to this peer
        :param peer_ip:
        :return:
        """
        # TODO: have a purge
        when = time.time()
        if peer_ip in self.peers_sent:
            # can be frozen, no need to lock and update, time is already in the future.
            if self.peers_sent[peer_ip] > when:
                return
        with self.peers_lock:
            self.peers_sent[peer_ip] = when

    def sendable(self, peer_ip):
        """
        Tells is the mempool is sendable to a given peers
        (ie, we sent it more than 30 sec ago)
        :param peer_ip:
        :return:
        """
        if peer_ip not in self.peers_sent:
            # New peer
            return True
        sendable = self.peers_sent[peer_ip] < time.time() - drift_limit
        # Temp
        if not sendable:
            pass
            # self.app_log.warning("Mempool not sendable for {} yet.".format(peer_ip))
        return sendable

    def tx_to_send(self, peer_ip, peer_txs=None):
        """
        Selects the Tx to be sent to a given peer
        :param peer_ip:
        :return:
        """
        if DEBUG_DO_NOT_SEND_TX:
            all = self.fetchall(SQL_SELECT_TX_TO_SEND)
            tx_count = len(all)
            tx_list = [tx[1]+' '+tx[2]+' : '+str(tx[3]) for tx in all]
            #print("I have {} txs for {} but won't send: {}".format(tx_count, peer_ip, "\n".join(tx_list)))
            print("I have {} txs for {} but won't send".format(tx_count, peer_ip))
            return []
        # Get our raw txs
        if peer_ip not in self.peers_sent:
            # new peer, never seen, send all
            raw = self.fetchall(SQL_SELECT_TX_TO_SEND)
        else:
            # add some margin to account for tx in the future, 5 sec ?
            last_sent = self.peers_sent[peer_ip]-5
            raw = self.fetchall(SQL_SELECT_TX_TO_SEND_SINCE, (last_sent, ))
        # Now filter out the tx we got from the peer
        if peer_txs:
            peers_sig = [tx[4] for tx in peer_txs]
            # TEMP
            #print("raw for", peer_ip, len(raw))
            #print("peers_sig", peer_ip, len(peers_sig))
            filtered = [tx for tx in raw if raw[4] not in peers_sig]
            # TEMP
            #print("filtered", peer_ip, len(filtered))
            return filtered
        else:
            return raw

    def merge(self, data, peer_ip, c, size_bypass=False, wait=False, revert=False):
        """
        Checks and merge the tx list in out mempool
        :param data:
        :param peer_ip:
        :param c:
        :param size_bypass: if True, will merge whatever the mempool size is
        :param wait: if True, will wait until the main db_lock is free. if False, will just drop.
        :param revert: if True, we are reverting tx from digest_block, so main lock is on. Don't bother, process without lock.
        :return:
        """
        if not data:
            return "Mempool from {} was empty".format(peer_ip)

        mempool_result = []

        if data == '*':
            raise ValueError("Connection lost")

        try:
            if self.peers_sent[peer_ip] > time.time():
                self.app_log.warning("Mempool ignoring merge from frozen {}".format(peer_ip))
                mempool_result.append("Mempool ignoring merge from frozen {}".format(peer_ip))
                return mempool_result
        except:
            # unknown peer
            pass

        if not essentials.is_sequence(data):
            with self.peers_lock:
                self.peers_sent[peer_ip] = time.time() + 10 * 60
            self.app_log.warning("Freezing mempool from {} for 10 min - Bad TX format".format(peer_ip))
            mempool_result.append("Bad TX Format")
            return mempool_result

        mempool_result.append("Mempool merging started from {}".format(peer_ip))

        if not revert:
            while self.db_lock.locked():  # prevent transactions which are just being digested from being added to mempool
                if not wait:
                    # not reverting, but not waiting, bye
                    # By default, we don't wait.
                    mempool_result.append("Locked ledger, dropping txs")
                    return mempool_result
                self.app_log.warning("Waiting for block digestion to finish before merging mempool")
                time.sleep(1)
        # if reverting, don't bother with main lock, go on.

        mempool_size = self.size()  # caulculate current mempool size before adding txs

        # TODO: we check main ledger db is not locked before beginning, but we don't lock? ok, see comment in node.py. since it's called from a lock, it would deadlock.
        # merge mempool
        #while self.lock.locked():
        #    time.sleep(1)
        with self.lock:
            try:
                block_list = data

                if not isinstance(block_list[0], list):  # convert to list of lists if only one tx and not handled
                    block_list = [block_list]

                for transaction in block_list:  # set means unique, only accepts list of txs

                    if (mempool_size < 0.3 or size_bypass) or \
                            (len(str(transaction[7])) > 200 and mempool_size < 0.4) \
                            or (Decimal(transaction[3]) > Decimal(5) and mempool_size < 0.5) \
                            or (transaction[1] in self.config.mempool_allowed and mempool_size < 0.6):
                        # condition 1: size limit or bypass,
                        # condition 2: spend more than 25 coins,
                        # condition 3: have length of openfield larger than 200
                        # all transactions in the mempool need to be cycled to check for special cases,
                        # therefore no while/break loop here

                        mempool_timestamp = '%.2f' % (quantize_two(transaction[0]))
                        mempool_address = str(transaction[1])[:56]
                        mempool_recipient = str(transaction[2])[:56]
                        mempool_amount = '%.8f' % (quantize_eight(transaction[3]))  # convert scientific notation
                        mempool_signature_enc = str(transaction[4])[:684]
                        mempool_public_key_hashed = str(transaction[5])[:1068]
                        mempool_operation = str(transaction[6])[:10]
                        mempool_openfield = str(transaction[7])[:100000]

                        # convert readable key to instance
                        mempool_public_key = RSA.importKey(base64.b64decode(mempool_public_key_hashed))
                        mempool_signature_dec = base64.b64decode(mempool_signature_enc)

                        acceptable = True

                        try:
                            # TODO: sure it will throw an exception?
                            # condition 1)
                            dummy = self.fetchall("SELECT * FROM transactions WHERE signature = ?;",
                                                  (mempool_signature_enc,))
                            if dummy:
                                # self.app_log.warning("That transaction is already in our mempool")
                                mempool_result.append("That transaction is already in our mempool")
                                acceptable = False
                                mempool_in = True
                            else:
                                mempool_in = False
                        except:
                            #print('sigmempool NO ', mempool_signature_enc)
                            mempool_in = False

                        # reject transactions which are already in the ledger
                        # TODO: not clean, will need to have ledger as a module too.
                        # dup code atm.
                        essentials.execute_param_c(c, "SELECT * FROM transactions WHERE signature = ?;",
                                        (mempool_signature_enc,), self.app_log)  # condition 2
                        try:
                            dummy = c.fetchall()[0]
                            #print('sigledger', mempool_signature_enc, dummy)
                            if dummy:
                                mempool_result.append("That transaction is already in our ledger")
                                # self.app_log.warning("That transaction is already in our ledger")
                                # reject transactions which are already in the ledger
                                acceptable = False
                                ledger_in = True
                                # Can be a syncing node. Do not request mempool from this peer until 10 min
                                with self.peers_lock:
                                    self.peers_sent[peer_ip] = time.time() + 10*60
                                self.app_log.warning("Freezing mempool from {} for 10 min.".format(peer_ip))
                                return mempool_result
                            else:
                                ledger_in = False
                        except:
                            #print('sigledger NO ', mempool_signature_enc)
                            ledger_in = False

                        # if mempool_operation != "1" and mempool_operation != "0":
                        #    mempool_result.append = ("Mempool: Wrong keep value {}".format(mempool_operation))
                        #    acceptable = 0

                        if mempool_address != hashlib.sha224(base64.b64decode(mempool_public_key_hashed)).hexdigest():
                            mempool_result.append("Mempool: Attempt to spend from a wrong address")
                            # self.app_log.warning("Mempool: Attempt to spend from a wrong address")
                            acceptable = False

                        if not essentials.address_validate(mempool_address) or not essentials.address_validate(mempool_recipient):
                            mempool_result.append("Mempool: Not a valid address")
                            # self.app_log.warning("Mempool: Not a valid address")
                            acceptable = False

                        if quantize_eight(mempool_amount) < 0:
                            acceptable = False
                            mempool_result.append("Mempool: Negative balance spend attempt")
                            # self.app_log.warning("Mempool: Negative balance spend attempt")

                        if quantize_two(mempool_timestamp) > time.time() + drift_limit:  # dont accept future txs
                            acceptable = False

                        # dont accept old txs, mempool needs to be harsher than ledger
                        if quantize_two(mempool_timestamp) < time.time() - 82800:
                            acceptable = 0
                        # remove from mempool if it's in both ledger and mempool already
                        if mempool_in and ledger_in:
                            try:
                                # Do not lock, we already have the lock for the whole merge.
                                self.execute(SQL_DELETE_TX, (mempool_signature_enc, ))
                                self.commit()
                                mempool_result.append("Mempool: Transaction deleted from our mempool")
                            except:  # experimental try and except
                                mempool_result.append("Mempool: Transaction was not present in the pool anymore")
                                pass  # continue to mempool finished message

                                # verify signatures and balances

                        essentials.validate_pem(mempool_public_key_hashed)

                        # verify signature
                        verifier = PKCS1_v1_5.new(mempool_public_key)

                        my_hash = SHA.new(str((mempool_timestamp, mempool_address, mempool_recipient, mempool_amount,
                                               mempool_operation, mempool_openfield)).encode("utf-8"))
                        if not verifier.verify(my_hash, mempool_signature_dec):
                            acceptable = False
                            mempool_result.append("Mempool: Wrong signature in mempool insert attempt: {}".
                                                  format(transaction))
                            # self.app_log.warning("Mempool: Wrong signature in mempool insert attempt")

                        # verify signature
                        if acceptable:

                            # verify balance
                            # mempool_result.append("Mempool: Verifying balance")
                            mempool_result.append("Mempool: Received address: {}".format(mempool_address))

                            # include mempool fees
                            result = self.fetchall("SELECT amount, openfield FROM transactions WHERE address = ?;",
                                                   (mempool_address, ))
                            debit_mempool = 0

                            if result:
                                for x in result:
                                    debit_tx = quantize_eight(x[0])
                                    fee = quantize_eight(essentials.fee_calculate(x[1]))
                                    debit_mempool = quantize_eight(debit_mempool + debit_tx + fee)
                            else:
                                debit_mempool = 0

                            # include the new block
                            credit_ledger = Decimal("0")
                            for entry in essentials.execute_param_c(c, "SELECT amount FROM transactions WHERE recipient = ?;",
                                                         (mempool_address, ), self.app_log):
                                try:
                                    credit_ledger = quantize_eight(credit_ledger) + quantize_eight(entry[0])
                                    credit_ledger = 0 if credit_ledger is None else credit_ledger
                                except:
                                    credit_ledger = 0

                            credit = credit_ledger

                            debit_ledger = Decimal("0")
                            for entry in essentials.execute_param_c(c, "SELECT amount FROM transactions WHERE address = ?;",
                                                         (mempool_address,), self.app_log):
                                try:
                                    debit_ledger = quantize_eight(debit_ledger) + quantize_eight(entry[0])
                                    debit_ledger = 0 if debit_ledger is None else debit_ledger
                                except:
                                    debit_ledger = 0

                            debit = debit_ledger + debit_mempool

                            fees = Decimal("0")
                            for entry in essentials.execute_param_c(c, "SELECT fee FROM transactions WHERE address = ?;",
                                                         (mempool_address, ), self.app_log):
                                try:
                                    fees = quantize_eight(fees) + quantize_eight(entry[0])
                                    fees = 0 if fees is None else fees
                                except:
                                    fees = 0

                            rewards = Decimal("0")
                            for entry in essentials.execute_param_c(c, "SELECT sum(reward) FROM transactions WHERE recipient = ?;",
                                                         (mempool_address, ), self.app_log):
                                try:
                                    rewards = quantize_eight(rewards) + quantize_eight(entry[0])
                                    rewards = 0 if rewards is None else rewards
                                except:
                                    rewards = 0

                            balance = quantize_eight(credit - debit - fees + rewards - quantize_eight(mempool_amount))
                            balance_pre = quantize_eight(credit_ledger - debit_ledger - fees + rewards)

                            fee = essentials.fee_calculate(mempool_openfield)

                            time_now = time.time()

                            if quantize_two(mempool_timestamp) > quantize_two(time_now) + drift_limit:
                                mempool_result.append(
                                    "Mempool: Future transaction not allowed, timestamp {} minutes in the future".
                                    format(quantize_two((quantize_two(mempool_timestamp) - quantize_two(time_now))
                                    / 60)))
                                # self.app_log.warning("Mempool: Future transaction not allowed, timestamp {} minutes in the future.")


                            elif quantize_two(time_now) - 86400 > quantize_two(mempool_timestamp):
                                mempool_result.append("Mempool: Transaction older than 24h not allowed.")
                                # self.app_log.warning("Mempool: Transaction older than 24h not allowed.")

                            elif quantize_eight(mempool_amount) > quantize_eight(balance_pre):
                                mempool_result.append("Mempool: Sending more than owned")
                                # self.app_log.warning("Mempool: Sending more than owned")
                            elif quantize_eight(balance) - quantize_eight(fee) < 0:
                                mempool_result.append("Mempool: Cannot afford to pay fees")
                                # self.app_log.warning("Mempool: Cannot afford to pay fees")

                            # verify signatures and balances
                            else:
                                self.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)",
                                             (str(mempool_timestamp), str(mempool_address), str(mempool_recipient),
                                              str(mempool_amount), str(mempool_signature_enc),
                                              str(mempool_public_key_hashed),
                                              str(mempool_operation), str(mempool_openfield)))
                                mempool_result.append("Mempool updated with a received transaction from {}".
                                                      format(peer_ip))
                                self.commit()  # Save (commit) the changes

                                mempool_size = mempool_size + sys.getsizeof(str(transaction)) / 1000000.0
                    else:
                        mempool_result.append("Local mempool is already full for this tx type, skipping merging")
                        # self.app_log.warning("Local mempool is already full for this tx type, skipping merging")
                        return mempool_result  # avoid spamming of the logs
                # TODO: Here maybe commit() on c to release the write lock?
            except Exception as e:
                self.app_log.warning("Mempool: Error processing: {} {}".format(data, e))
                if self.config.debug_conf == 1:
                    raise
        try:
            return e, mempool_result
        except:
            return mempool_result