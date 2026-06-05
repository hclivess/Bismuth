"""
Mempool read/reporting & maintenance queries, split out of the Mempool class as a mixin and recombined
via ``class Mempool(MempoolQueriesMixin)``. Methods are byte-identical and run on the composed Mempool
instance (``self.fetchall``/``self.execute``/``self.commit`` come from the core).
"""
import sys
import time

from mempool_sql import (
    DEBUG_DO_NOT_SEND_TX,
    SQL_CLEAR,
    SQL_DELETE_TX,
    SQL_DELETE_TX_OLD,
    SQL_MEMPOOL_GET,
    SQL_PURGE,
    SQL_SELECT_ALL_VALID_TXS,
    SQL_SELECT_TX_TO_SEND,
    SQL_SELECT_TX_TO_SEND_SINCE,
    SQL_SIG_CHECK,
    SQL_SIG_CHECK_OLD,
    SQL_STATUS,
)


class MempoolQueriesMixin:
    def mp_get(self, balance_address):
        """
        base mempool
        :return:
        """
        return self.fetchall(SQL_MEMPOOL_GET, (balance_address,))

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
