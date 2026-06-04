"""
Mempool module for Bismuth nodes
"""

import functools
# import base64
# import hashlib
import os
import sqlite3
import sys
import threading
from decimal import Decimal
import time

from polysign.signerfactory import SignerFactory

import essentials
from quantizer import quantize_two, quantize_eight, quantize_ten

# from Cryptodome.Hash import SHA
# from Cryptodome.PublicKey import RSA
# from Cryptodome.Signature import PKCS1_v1_5

__version__ = "0.0.7e"

"""
0.0.5g - Add default param to mergedts for compatibility
0.0.5f - Using polysign
0.0.5e - add mergedts timestamp to tx for better handling of late txs
         quicker unfreeze
         less strict freezing
0.0.6b - Raise freeze tolerance to > 15 minutes old txs.
0.0.6c - Return last exception to client in all cases
0.0.7a - Add support for mandatory message addresses
0.0.7b - Reduce age of valid txns to 2 hours
0.0.7c - Remove unnecessary Decimal
0.0.7e - exclude too old txs from mempool balance, simplify mempool merge.
"""

DECIMAL0 = Decimal(0)

MEMPOOL = None

# If set to true, will always send empty Tx to other peers (but will accept theirs)
# Only to be used for debug/testing purposes
DEBUG_DO_NOT_SEND_TX = False

# Tx age limit (in seconds) - Default 82800
# REFUSE_OLDER_THAN = 82800
REFUSE_OLDER_THAN = 60 * 60 * 2  # reduced to 2 hours
# See also SQL_PURGE, SQL_MEMPOOL_GET and SQL_SELECT_ALL_VALID_TXS a few lines down.
# I used a filter on some requests rather than calling purge() every time.
# Maybe a systematic purge() would be easier and faster. To be tested.

# How long for freeze nodes that send late enough tx we already have in ledger
FREEZE_MIN = 5

"""
Common Sql requests
"""

# Create mempool table
SQL_CREATE = "CREATE TABLE IF NOT EXISTS transactions (" \
             "timestamp TEXT, address TEXT, recipient TEXT, amount TEXT, signature TEXT, " \
             "public_key TEXT, operation TEXT, openfield TEXT, mergedts INTEGER(4) not null default (strftime('%s','now')) )"

# Purge old txs that may be stuck
SQL_PURGE = "DELETE FROM transactions WHERE timestamp <= strftime('%s', 'now', '-2 hour')"

# Delete all transactions
SQL_CLEAR = "DELETE FROM transactions"

# Check for presence of a given tx signature
SQL_SIG_CHECK = 'SELECT timestamp FROM transactions WHERE substr(signature,1,4) = substr(?1,1,4) and signature = ?1'
SQL_SIG_CHECK_OLD = 'SELECT timestamp FROM transactions WHERE signature = ?1'

# delete a single tx
SQL_DELETE_TX = 'DELETE FROM transactions WHERE substr(signature,1,4) = substr(?1,1,4) and signature = ?1'
SQL_DELETE_TX_OLD = 'DELETE FROM transactions WHERE signature = ?1'

# Selects all tx from mempool - list fields so we don't send mergedts and keep compatibility
SQL_SELECT_ALL_TXS = 'SELECT timestamp, address, recipient, amount, signature, public_key, operation, openfield FROM transactions'

# Selects all tx from mempool - list fields so we don't send mergedts and keep compatibility
SQL_SELECT_ALL_VALID_TXS = "SELECT timestamp, address, recipient, amount, signature, public_key, operation, openfield FROM transactions WHERE timestamp > strftime('%s', 'now', '-2 hour')"

# Counts distinct senders from mempool
SQL_COUNT_DISTINCT_SENDERS = 'SELECT COUNT(DISTINCT(address)) FROM transactions'

# Counts distinct recipients from mempool
SQL_COUNT_DISTINCT_RECIPIENTS = 'SELECT COUNT(DISTINCT(recipient)) FROM transactions'

# A single requets for status info
SQL_STATUS = 'SELECT COUNT(*) AS nb, SUM(LENGTH(openfield)) AS len, COUNT(DISTINCT(address)) as senders, COUNT(DISTINCT(recipient)) as recipients FROM transactions'

# Select Tx to be sent to a peer
SQL_SELECT_TX_TO_SEND = 'SELECT * FROM transactions ORDER BY amount DESC'

# Select Tx to be sent to a peer since the given ts - what counts is the merged time, not the tx time.
SQL_SELECT_TX_TO_SEND_SINCE = 'SELECT * FROM transactions where mergedts > ? ORDER BY amount DESC'

SQL_MEMPOOL_GET = "SELECT amount, openfield, operation FROM transactions WHERE address = ? and timestamp > strftime('%s', 'now', '-2 hour')"


def sql_trace_callback(log, id, statement):
    line = f"SQL[{id}] {statement}"
    log.warning(line)


class Mempool:
    """The mempool manager. Thread safe"""

    def __init__(self, app_log, config=None, db_lock=None, testnet=False, trace_db_calls=True):
        try:
            self.app_log = app_log
            self.config = config
            self.db_lock = db_lock
            self.ram = self.config.mempool_ram
            if self.config.version == 'regnet':
                self.app_log.warning("Regtest mode, ram mempool")
                self.ram = True

            self.lock = threading.Lock()
            self.peers_lock = threading.Lock()
            # ip: last time sent
            self.peers_sent = dict()
            self.db = None
            self.cursor = None
            self.trace_db_calls = trace_db_calls

            self.testnet = testnet

            if self.testnet:
                app_log.warning("Starting mempool in testnet mode")
                self.mempool_path = "mempool_testnet.db"
                self.mempool_ram_file = "file:mempool_testnet?mode=memory&cache=shared"
            else:
                self.mempool_ram_file = "file:mempool?mode=memory&cache=shared"
                self.mempool_path = self.config.mempool_path #default

            self.check()

        except Exception as e:
            self.app_log.error("Error creating mempool: {}".format(e))
            raise

    def mp_get(self, balance_address):
        """
        base mempool
        :return:
        """
        return self.fetchall(SQL_MEMPOOL_GET, (balance_address,))

    def check(self):
        """
        Checks if mempool exists, create if not.
        :return:
        """
        self.app_log.warning("Mempool Check")
        with self.lock:
            if self.ram:
                self.db = sqlite3.connect(self.mempool_ram_file,
                                          uri=True, timeout=1, isolation_level=None,
                                          check_same_thread=False)
                if self.trace_db_calls:
                    self.db.set_trace_callback(functools.partial(sql_trace_callback,self.app_log,"MEMPOOL-RAM"))
                self.db.execute('PRAGMA journal_mode = WAL;')
                self.db.execute("PRAGMA page_size = 4096;")
                self.db.text_factory = str
                self.cursor = self.db.cursor()
                self.cursor.execute(SQL_CREATE)
                self.db.commit()
                self.app_log.warning("Status: In memory mempool file created")
            else:
                self.db = sqlite3.connect(self.mempool_path, timeout=1,
                                          check_same_thread=False)
                if self.trace_db_calls:
                    self.db.set_trace_callback(functools.partial(sql_trace_callback,self.app_log,"MEMPOOL"))
                self.db.text_factory = str
                self.cursor = self.db.cursor()

                # check if mempool needs recreating
                self.cursor.execute("PRAGMA table_info('transactions')")
                res = self.cursor.fetchall()
                # print(res)
                if len(res) != 9:
                    self.db.close()
                    os.remove(self.mempool_path)
                    self.db = sqlite3.connect(self.mempool_path, timeout=1,
                                              check_same_thread=False)
                    if self.trace_db_calls:
                        self.db.set_trace_callback(functools.partial(sql_trace_callback,self.app_log,"MEMPOOL"))
                    self.db.text_factory = str
                    self.cursor = self.db.cursor()
                    self.execute(SQL_CREATE)
                    self.commit()
                    self.app_log.warning("Status: Recreated mempool file")

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
        with self.lock:
            self.app_log.warning("Purging mempool")
            try:
                self.execute(SQL_PURGE)
                self.commit()
            except Exception as e:
                self.app_log.error("Error {} on mempool purge".format(e))

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
            if self.config.old_sqlite:
                self.execute(SQL_DELETE_TX_OLD, (signature,))
            else:
                self.execute(SQL_DELETE_TX, (signature,))
            self.commit()

    def sig_check(self, signature):
        """
        Returns presence of the sig in the mempool
        :param signature:
        :return: boolean
        """
        if self.config.old_sqlite:
            return bool(self.fetchone(SQL_SIG_CHECK_OLD, (signature,)))
        else:
            return bool(self.fetchone(SQL_SIG_CHECK, (signature,)))

    def status(self):
        """
        Stats on the current mempool
        :return: tuple(tx#, openfield len, distinct sender#, distinct recipients#
        """
        try:
            limit = time.time()
            frozen = [peer for peer in self.peers_sent if self.peers_sent[peer] > limit]
            self.app_log.warning("Status: MEMPOOL Frozen = {}".format(", ".join(frozen)))
            # print(limit, self.peers_sent, frozen)
            # Cleanup old nodes not synced since 15 min
            limit = limit - 15 * 60
            with self.peers_lock:
                self.peers_sent = {peer: self.peers_sent[peer] for peer in self.peers_sent if
                                   self.peers_sent[peer] > limit}
            self.app_log.warning(
                "Status: MEMPOOL Live = {}".format(", ".join(set(self.peers_sent.keys()) - set(frozen))))
            status = self.fetchall(SQL_STATUS)
            count, open_len, senders, recipients = status[0]
            self.app_log.warning(
                "Status: MEMPOOL {} Txs from {} senders to {} distinct recipients. Openfield len {}".
                    format(count, senders, recipients, open_len))
            return status[0]
        except:
            return 0

    def size(self):
        """
        Curent size of the mempool in Mo
        :return:
        """
        try:
            mempool_txs = self.fetchall(SQL_SELECT_ALL_VALID_TXS)
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
        sendable = self.peers_sent[peer_ip] < time.time() - 30
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
            tx_list = [tx[1] + ' ' + tx[2] + ' : ' + str(tx[3]) for tx in all]
            # print("I have {} txs for {} but won't send: {}".format(tx_count, peer_ip, "\n".join(tx_list)))
            print("I have {} txs for {} but won't send".format(tx_count, peer_ip))
            return []
        # Get our raw txs
        if peer_ip not in self.peers_sent:
            # new peer, never seen, send all
            raw = self.fetchall(SQL_SELECT_TX_TO_SEND)
        else:
            # add some margin to account for tx in the future, 5 sec ?
            last_sent = self.peers_sent[peer_ip] - 5
            raw = self.fetchall(SQL_SELECT_TX_TO_SEND_SINCE, (last_sent,))
        # Now filter out the tx we got from the peer
        if peer_txs:
            peers_sig = [tx[4] for tx in peer_txs]
            # TEMP
            # print("raw for", peer_ip, len(raw))
            # print("peers_sig", peer_ip, len(peers_sig))

            filtered = [tx for tx in raw if tx[4] not in peers_sig]
            # TEMP
            # print("filtered", peer_ip, len(filtered))
            return filtered
        else:
            return raw

    def space_left_for_tx(self, transaction, mempool_size):
        """
        Tells if we should let a specific tx in, depending on space left and its characteristics.
        :param transaction:
        :param mempool_size:
        :param size_bypass:
        :return:
        """
        # Allow whatever the tx is
        if mempool_size < 0.3:
            return True
        # Low priority tx, token or openfield data
        if mempool_size < 0.4:
            if len(str(transaction[7])) > 200:
                # Openfield > 200
                return True
            if "token:" == transaction[6][:6]:
                return True
        # Medium prio: 5 BIS or more
        if mempool_size < 0.5:
            if float(transaction[3]) > 5:
                return True
        # High prio: allowed by config
        if mempool_size < 0.6:
            if transaction[1] in self.config.mempool_allowed:
                return True
        # Sorry, no space left for this tx type.
        return False

    def merge(self, data: list, peer_ip: str, c, size_bypass: bool=False, wait: bool=False, revert: bool =False) -> list:
        """
        Checks and merge the tx list in our mempool
        :param data:
        :param peer_ip:
        :param c:
        :param size_bypass: if True, will merge whatever the mempool size is
        :param wait: if True, will wait until the main db_lock is free. if False, will just drop.
        :param revert: if True, we are reverting tx from digest_block, so main lock is on. Don't bother, process without lock.
        :return:
        """
        global REFUSE_OLDER_THAN
        # Easy cases of empty or invalid data
        if not data:
            return ["Mempool from {} was empty".format(peer_ip)]
        mempool_result = []
        if data == '*':
            raise ValueError("Connection lost")
        try:
            if self.peers_sent[peer_ip] > time.time() and peer_ip != '127.0.0.1':
                self.app_log.warning("Mempool ignoring merge from frozen {}".format(peer_ip))
                mempool_result.append("Mempool ignoring merge from frozen {}".format(peer_ip))
                return mempool_result
        except:
            # unknown peer
            pass
        if not essentials.is_sequence(data):
            if peer_ip != '127.0.0.1':
                with self.peers_lock:
                    self.peers_sent[peer_ip] = time.time() + 10 * 60
                self.app_log.warning("Freezing mempool from {} for 10 min - Bad TX format".format(peer_ip))
            mempool_result.append("Bad TX Format")
            return mempool_result

        if not revert:
            while self.db_lock.locked():
                # prevent transactions which are just being digested from being added to mempool
                if not wait:
                    # not reverting, but not waiting, bye
                    # By default, we don't wait.
                    mempool_result.append("Locked ledger, dropping txs")
                    return mempool_result
                self.app_log.warning("Waiting for block digestion to finish before merging mempool")
                time.sleep(1)
        # if reverting, don't bother with main lock, go on.
        # Let's really dig
        mempool_result.append("Mempool merging started from {}".format(peer_ip))
        # Single time reference here for the whole merge.
        time_now = time.time()
        # calculate current mempool size before adding txs
        mempool_size = self.size()

        # TODO: we check main ledger db is not locked before beginning, but we don't lock? ok, see comment in node.py. since it's called from a lock, it would deadlock.
        # merge mempool
        # while self.lock.locked():
        #    time.sleep(1)
        with self.lock:
            try:
                block_list = data
                if not isinstance(block_list[0], list):  # convert to list of lists if only one tx and not handled
                    block_list = [block_list]

                for transaction in block_list:
                    if size_bypass or self.space_left_for_tx(transaction, mempool_size):
                        # all transactions in the mempool need to be cycled to check for special cases,
                        # therefore no while/break loop here
                        try:
                            mempool_timestamp = '%.2f' % (quantize_two(transaction[0]))
                            mempool_timestamp_float = float(transaction[0])  # limit Decimal where not needed
                        except Exception as e:
                            mempool_result.append("Mempool: Invalid timestamp {}".format(transaction[0]))
                            continue
                        if not essentials.address_validate(transaction[1]):
                            mempool_result.append("Mempool: Invalid address {}".format(transaction[1]))
                            continue
                        # We could now ignore the truncates here, I left them for explicit reminder of the various fields max lengths.
                        mempool_address = str(transaction[1])[:56]
                        if not essentials.address_validate(transaction[2]):
                            mempool_result.append("Mempool: Invalid recipient {}".format(transaction[2]))
                            continue
                        mempool_recipient = str(transaction[2])[:56]
                        try:
                            mempool_amount = '%.8f' % (quantize_eight(transaction[3]))  # convert scientific notation
                            mempool_amount_float = float(transaction[3])
                        except Exception as e:
                            mempool_result.append("Mempool: Invalid amount {}".format(transaction[3]))
                            continue
                        if len(transaction[4]) > 684:
                            mempool_result.append("Mempool: Invalid signature len{}".format(len(transaction[4])))
                            continue
                        mempool_signature_enc = str(transaction[4])[:684]
                        if len(transaction[5]) > 1068:
                            mempool_result.append("Mempool: Invalid pubkey len{}".format(len(transaction[5])))
                            continue
                        mempool_public_key_b64encoded = str(transaction[5])[:1068]
                        if "b'" == mempool_public_key_b64encoded[:2]:
                            # Binary content instead of str - leftover from legacy code?
                            mempool_public_key_b64encoded = transaction[5][2:1070]
                        if len(transaction[6]) > 30:
                            mempool_result.append("Mempool: Invalid operation len{}".format(len(transaction[6])))
                            continue
                        mempool_operation = str(transaction[6])[:30]
                        if len(transaction[7]) > 100000:
                            mempool_result.append("Mempool: Invalid openfield len{}".format(len(transaction[7])))
                            continue
                        mempool_openfield = str(transaction[7])[:100000]

                        if len(mempool_openfield) <= 4:
                            # no or short message for a mandatory message
                            if mempool_recipient in self.config.mandatory_message.keys():
                                mempool_result.append("Mempool: Missing message - {}".format(self.config.mandatory_message[mempool_recipient]))
                                continue

                        # Begin with the easy tests that do not require cpu or disk access
                        if mempool_amount_float < 0:
                            mempool_result.append("Mempool: Negative balance spend attempt")
                            continue
                        if mempool_timestamp_float > time_now:
                            mempool_result.append("Mempool: Future transaction rejected {}s".
                                                  format(mempool_timestamp_float - time_now))
                            continue
                        if mempool_timestamp_float < time_now - REFUSE_OLDER_THAN:
                            # don't accept old txs, mempool needs to be harsher than ledger
                            mempool_result.append("Mempool: Too old a transaction")
                            continue

                        # Then more cpu heavy tests
                        buffer = str((mempool_timestamp, mempool_address, mempool_recipient, mempool_amount,
                                      mempool_operation, mempool_openfield)).encode("utf-8")

                        #  Will raise if error
                        try:
                            SignerFactory.verify_bis_signature(mempool_signature_enc, mempool_public_key_b64encoded,
                                                               buffer,
                                                               mempool_address)
                        except Exception as e:
                            mempool_result.append(f"Mempool: Signature did not match for address ({e})")
                            continue

                        # Only now, process the tests requiring db access
                        mempool_in = self.sig_check(mempool_signature_enc)

                        # Temp: get last block for HF reason
                        essentials.execute_param_c(c, "SELECT block_height FROM transactions WHERE 1 ORDER by block_height DESC limit ?",
                                                   (1,), self.app_log)
                        last_block = c.fetchone()[0]
                        # reject transactions which are already in the ledger
                        # TODO: not clean, will need to have ledger as a module too.
                        # TODO: need better txid index, this is very sloooooooow
                        if self.config.old_sqlite:
                            essentials.execute_param_c(c, "SELECT timestamp FROM transactions WHERE signature = ?1",
                                                       (mempool_signature_enc,), self.app_log)
                        else:
                            essentials.execute_param_c(c,
                                                       "SELECT timestamp FROM transactions WHERE substr(signature,1,4) = substr(?1,1,4) AND signature = ?1",
                                                       (mempool_signature_enc,), self.app_log)
                        ledger_in = bool(c.fetchone())
                        # remove from mempool if it's in both ledger and mempool already
                        if mempool_in and ledger_in:
                            try:
                                # Do not lock, we already have the lock for the whole merge.
                                if self.config.old_sqlite:
                                    self.execute(SQL_DELETE_TX_OLD, (mempool_signature_enc,))
                                else:
                                    self.execute(SQL_DELETE_TX, (mempool_signature_enc,))
                                self.commit()
                                mempool_result.append("Mempool: Transaction deleted from our mempool")
                            except:  # experimental try and except
                                mempool_result.append("Mempool: Transaction was not present in the pool anymore")
                            continue
                        if ledger_in:
                            mempool_result.append("That transaction is already in our ledger")
                            # Can be a syncing node. Do not request mempool from this peer until FREEZE_MIN min
                            # ledger_in is the ts of the tx in ledger. if it's recent, maybe the peer is just one block late.
                            # give him 15 minute margin.
                            if (peer_ip != '127.0.0.1') and (ledger_in < time_now - 60 * 15):
                                with self.peers_lock:
                                    self.peers_sent[peer_ip] = time.time() + FREEZE_MIN * 60
                                self.app_log.warning("Freezing mempool from {} for {} min.".format(peer_ip, FREEZE_MIN))
                            # Here, we point blank stop processing the batch from this host since it's outdated.
                            # Update: Do not, since it blocks further valid tx - case has been found in real use.
                            # return mempool_result
                            continue
                        # Already there, just ignore then
                        if mempool_in:
                            mempool_result.append("That transaction is already in our mempool")
                            continue

                        # Here we covered the basics, the current tx is conform and signed. Now let's check balance.

                        # verify balance
                        mempool_result.append("Mempool: Received address: {}".format(mempool_address))
                        # include mempool fees - excluding the old ones.
                        result = self.fetchall(SQL_MEMPOOL_GET, (mempool_address, ))
                        debit_mempool = DECIMAL0
                        if result:
                            for x in result:
                                debit_tx = quantize_eight(x[0])
                                fee = essentials.fee_calculate(x[1], x[2], last_block)  # fee_calculate sends back a Decimal 8
                                debit_mempool += debit_tx + fee

                        credit = DECIMAL0
                        rewards = DECIMAL0
                        for entry in essentials.execute_param_c(c,
                                                                "SELECT amount, reward FROM transactions WHERE recipient = ?",
                                                                (mempool_address, ), self.app_log):
                            credit += quantize_eight(entry[0])
                            rewards += quantize_eight(entry[1])

                        debit_ledger = DECIMAL0
                        fees = DECIMAL0
                        for entry in essentials.execute_param_c(c,
                                                                "SELECT amount, fee FROM transactions WHERE address = ?",
                                                                (mempool_address,), self.app_log):
                            debit_ledger += quantize_eight(entry[0])
                            fees += quantize_eight(entry[1])

                        debit = debit_ledger + debit_mempool

                        # both are Decimals
                        balance = credit - debit - fees + rewards - quantize_eight(mempool_amount)
                        balance_pre = credit - debit_ledger - fees + rewards

                        fee = essentials.fee_calculate(mempool_openfield, mempool_operation, last_block)

                        # print("Balance", balance, fee)

                        if quantize_eight(mempool_amount) > balance_pre:  # mempool_amount is a 0.8f string for some reason
                            # mp amount is already included in "balance" var! also, that tx might already be in the mempool
                            mempool_result.append("Mempool: Sending more than owned")
                            continue
                        if balance - fee < 0:
                            mempool_result.append("Mempool: Cannot afford to pay fees")
                            continue

                        # Pfew! we can finally insert into mempool - all is str, type converted and enforced above
                        self.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?)",
                                     (mempool_timestamp, mempool_address, mempool_recipient, mempool_amount,
                                      mempool_signature_enc, mempool_public_key_b64encoded, mempool_operation,
                                      mempool_openfield, int(time_now)))
                        mempool_result.append("Mempool updated with a received transaction from {}".format(peer_ip))
                        mempool_result.append("Success")  # WARNING: Do not change string or case ever!
                        self.commit()  # Save (commit) the changes to mempool db

                        mempool_size += sys.getsizeof(str(transaction)) / 1000000.0
                    else:
                        mempool_result.append("Local mempool is already full for this tx type, skipping merging")
                        # self.app_log.warning("Local mempool is already full for this tx type, skipping merging")
                # TEMP
                # print("Mempool insert", mempool_result)
                return mempool_result
                # TODO: Here maybe commit() on c to release the write lock?
            except Exception as e:
                self.app_log.warning("Mempool: Error processing: {} {}".format(data, e))
                if self.config.debug:
                    exc_type, exc_obj, exc_tb = sys.exc_info()
                    fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                    self.app_log.warning("{} {} {}".format(exc_type, fname, exc_tb.tb_lineno))
                    mempool_result.append("Exception: {}".format(str(e)))
                    # if left there, means debug can *not* be used in production, or exception is not sent back to the client.
                    raise
        return mempool_result
