"""
Mempool module for Bismuth nodes — the pending-transaction pool, held in memory.

Transactions are kept in an insertion-ordered dict keyed by signature, each with the time it was merged
(``mergedts``) so peers are only sent what is new to them. Nothing here is consensus: the digester
re-validates every transaction it includes. Thread safe (``self.lock`` guards every mutation).
"""

import block_store
import os
import sys
import threading
import time
from decimal import Decimal

from polysign.signerfactory import SignerFactory

import essentials
from quantizer import quantize_two, quantize_eight

__version__ = "0.1.0"

DECIMAL0 = Decimal(0)

# If set to true, will always send empty Tx to other peers (but will accept theirs)
# Only to be used for debug/testing purposes
DEBUG_DO_NOT_SEND_TX = False

# Tx age limit (in seconds)
REFUSE_OLDER_THAN = 60 * 60 * 2  # 2 hours

# How long for freeze nodes that send late enough tx we already have in ledger
FREEZE_MIN = 5

MEMPOOL = None

# A pooled tx: the 8 wire fields + the merge time
_TS, _ADDR, _RECIP, _AMOUNT, _SIG, _PUB, _OP, _OF, _MERGED = range(9)


class Mempool:
    """The mempool manager. Thread safe"""

    def __init__(self, app_log, config=None, db_lock=None, testnet=False, node=None):
        self.app_log = app_log
        # Live authoritative source for the hf2 mirror fields (fork_height / pk_registry / base_fee /
        # fee_post_fork). The digester also pushes these into self.* each block; reading through node closes
        # the window between process start and the first digest. None (tests) => the self.* mirror.
        self.node = node
        self.config = config
        self.db_lock = db_lock
        self.lock = threading.Lock()
        self.peers_lock = threading.Lock()
        # ip: last time sent
        self.peers_sent = dict()
        self.fork_height = None
        self.pk_registry = None
        self.base_fee = None
        self.fee_post_fork = False
        self.testnet = testnet
        self._txs = {}          # signature -> 9-tuple
        app_log.warning("Status: In memory mempool created")

    def _hf(self, name):
        if self.node is not None:
            return getattr(self.node, name, getattr(self, name))
        return getattr(self, name)

    def _ledger(self, ledger):
        """The ledger store from whatever the caller handed us (a DbHandler, the store, or None -> node)."""
        if ledger is None and self.node is not None:
            ledger = getattr(self.node, "ledger", None)
        return block_store.as_ledger(ledger)

    # ------------------------------------------------------------------ reads
    def transactions(self, since=None):
        """All pooled txs as 9-tuples (8 wire fields + mergedts), largest amount first. ``since``: only txs
        merged after that time."""
        with self.lock:
            rows = list(self._txs.values())
        if since is not None:
            rows = [r for r in rows if r[_MERGED] > since]
        return sorted(rows, key=lambda r: float(r[_AMOUNT]), reverse=True)

    def wire_transactions(self):
        """All pooled txs in the 8-field wire form, in pool order."""
        with self.lock:
            return [r[:8] for r in self._txs.values()]

    def get(self, signature):
        with self.lock:
            return self._txs.get(signature)

    def find_openfield(self, openfield):
        """The timestamp of a pooled tx carrying exactly ``openfield``, or None."""
        with self.lock:
            for r in self._txs.values():
                if r[_OF] == openfield:
                    return r[_TS]
        return None

    def by_address(self, address, valid_only=True):
        cutoff = time.time() - REFUSE_OLDER_THAN
        with self.lock:
            return [r for r in self._txs.values()
                    if (r[_ADDR] == address or r[_RECIP] == address)
                    and (not valid_only or float(r[_TS]) > cutoff)]

    def mp_get(self, balance_address):
        """(amount, openfield, operation) of the address's pending sends younger than the age limit."""
        cutoff = time.time() - REFUSE_OLDER_THAN
        with self.lock:
            return [(r[_AMOUNT], r[_OF], r[_OP]) for r in self._txs.values()
                    if r[_ADDR] == balance_address and float(r[_TS]) > cutoff]

    def sig_check(self, signature):
        with self.lock:
            return signature in self._txs

    def count(self):
        with self.lock:
            return len(self._txs)

    def status(self):
        """Stats on the current mempool: (tx#, openfield len, distinct sender#, distinct recipient#)."""
        try:
            limit = time.time()
            with self.peers_lock:
                peers_snapshot = dict(self.peers_sent)
            frozen = [peer for peer in peers_snapshot if peers_snapshot[peer] > limit]
            self.app_log.warning("Status: MEMPOOL Frozen = {}".format(", ".join(frozen)))
            limit = limit - 15 * 60
            with self.peers_lock:
                self.peers_sent = {peer: self.peers_sent[peer] for peer in self.peers_sent if
                                   self.peers_sent[peer] > limit}
            self.app_log.warning(
                "Status: MEMPOOL Live = {}".format(", ".join(set(peers_snapshot.keys()) - set(frozen))))
            with self.lock:
                rows = list(self._txs.values())
            count = len(rows)
            open_len = sum(len(r[_OF]) for r in rows) if rows else None
            senders = len({r[_ADDR] for r in rows})
            recipients = len({r[_RECIP] for r in rows})
            self.app_log.warning(
                "Status: MEMPOOL {} Txs from {} senders to {} distinct recipients. Openfield len {}".
                format(count, senders, recipients, open_len))
            return count, open_len, senders, recipients
        except Exception:
            return (0, 0, 0, 0)

    def size(self):
        """Current size of the (still valid) mempool in MB."""
        cutoff = time.time() - REFUSE_OLDER_THAN
        with self.lock:
            valid = [r[:8] for r in self._txs.values() if float(r[_TS]) > cutoff]
        return sys.getsizeof(str(valid)) / 1000000.0

    # ------------------------------------------------------------------ maintenance
    def purge(self):
        """Drop txs older than the age limit."""
        cutoff = time.time() - REFUSE_OLDER_THAN
        with self.lock:
            self.app_log.warning("Purging mempool")
            for sig in [s for s, r in self._txs.items() if float(r[_TS]) <= cutoff]:
                del self._txs[sig]

    def clear(self):
        with self.lock:
            self._txs.clear()

    def delete_transaction(self, signature):
        with self.lock:
            self._txs.pop(signature, None)

    def close(self):
        pass

    # ------------------------------------------------------------------ peers
    def sent(self, peer_ip):
        """Record the time of the last mempool send to this peer."""
        when = time.time()
        with self.peers_lock:
            if self.peers_sent.get(peer_ip, 0) <= when:
                self.peers_sent[peer_ip] = when

    def sendable(self, peer_ip):
        """Is the mempool sendable to this peer (sent more than 30 s ago)?"""
        if peer_ip not in self.peers_sent:
            return True
        return self.peers_sent[peer_ip] < time.time() - 30

    def tx_to_send(self, peer_ip, peer_txs=None):
        """The txs to send to a peer: everything merged since we last sent to it, minus what it sent us."""
        if DEBUG_DO_NOT_SEND_TX:
            print("I have {} txs for {} but won't send".format(self.count(), peer_ip))
            return []
        if peer_ip not in self.peers_sent:
            raw = self.transactions()
        else:
            # add some margin to account for tx in the future, 5 sec
            raw = self.transactions(since=self.peers_sent[peer_ip] - 5)
        if peer_txs:
            peers_sig = {tx[4] for tx in peer_txs}
            return [tx for tx in raw if tx[4] not in peers_sig]
        return raw

    def space_left_for_tx(self, transaction, mempool_size):
        """Tells if we should let a specific tx in, depending on space left and its characteristics.

        Anti-spam (doc/17): as the pool fills, admission tightens to higher-FEE txs. Priority is the
        tx's actual deterministic fee — the one thing a spammer cannot inflate without paying it —
        NOT the nominal `amount` and NOT a per-address allow-list. transaction[6]=operation, [7]=openfield.
        """
        if mempool_size < 0.3:
            return True
        fee = float(essentials.fee_calculate(transaction[7], transaction[6]))
        if mempool_size < 0.4:
            return fee > float(essentials.BASE_FEE)
        if mempool_size < 0.5:
            return fee >= 1.0
        if mempool_size < 0.6:
            return fee >= 10.0
        return False

    # ------------------------------------------------------------------ merge
    def merge(self, data: list, peer_ip: str, ledger=None, size_bypass: bool = False, wait: bool = False,
              revert: bool = False) -> list:
        """
        Checks and merge the tx list in our mempool
        :param data:
        :param peer_ip:
        :param ledger: the ledger (DbHandler / BlockStore); None -> node.ledger
        :param size_bypass: if True, will merge whatever the mempool size is
        :param wait: if True, will wait until the main db_lock is free. if False, will just drop.
        :param revert: if True, we are reverting tx from digest_block, so main lock is on.
        :return:
        """
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
        except KeyError:
            pass
        if not essentials.is_sequence(data):
            if peer_ip != '127.0.0.1':
                with self.peers_lock:
                    self.peers_sent[peer_ip] = time.time() + 10 * 60
                self.app_log.warning("Freezing mempool from {} for 10 min - Bad TX format".format(peer_ip))
            mempool_result.append("Bad TX Format")
            return mempool_result

        if not revert:
            while self.db_lock is not None and self.db_lock.locked():
                # prevent transactions which are just being digested from being added to mempool
                if not wait:
                    mempool_result.append("Locked ledger, dropping txs")
                    return mempool_result
                self.app_log.warning("Waiting for block digestion to finish before merging mempool")
                time.sleep(1)

        store = self._ledger(ledger)
        mempool_result.append("Mempool merging started from {}".format(peer_ip))
        time_now = time.time()
        mempool_size = self.size()

        with self.lock:
            try:
                block_list = data
                if not isinstance(block_list[0], list):  # a single tx
                    block_list = [block_list]

                for transaction in block_list:
                    if not (size_bypass or self.space_left_for_tx(transaction, mempool_size)):
                        mempool_result.append("Local mempool is already full for this tx type, skipping merging")
                        continue
                    try:
                        mempool_timestamp = '%.2f' % (quantize_two(transaction[0]))
                        mempool_timestamp_float = float(transaction[0])
                    except Exception:
                        mempool_result.append("Mempool: Invalid timestamp {}".format(transaction[0]))
                        continue
                    if not essentials.address_validate(transaction[1]):
                        mempool_result.append("Mempool: Invalid address {}".format(transaction[1]))
                        continue
                    mempool_address = str(transaction[1])[:56]
                    if not essentials.address_validate(transaction[2]):
                        mempool_result.append("Mempool: Invalid recipient {}".format(transaction[2]))
                        continue
                    mempool_recipient = str(transaction[2])[:56]
                    try:
                        # consensus boundary: the legacy '%.8f' decimal-string amount the signature covers
                        mempool_amount = '%.8f' % (quantize_eight(transaction[3]))
                        mempool_amount_float = float(transaction[3])
                    except Exception:
                        mempool_result.append("Mempool: Invalid amount {}".format(transaction[3]))
                        continue
                    if len(transaction[4]) > essentials.MAX_TX_SIGNATURE_LEN:
                        mempool_result.append("Mempool: Invalid signature len{}".format(len(transaction[4])))
                        continue
                    mempool_signature_enc = str(transaction[4])[:essentials.MAX_TX_SIGNATURE_LEN]
                    if len(transaction[5]) > essentials.MAX_TX_PUBKEY_LEN:
                        mempool_result.append("Mempool: Invalid pubkey len{}".format(len(transaction[5])))
                        continue
                    mempool_public_key_b64encoded = str(transaction[5])[:essentials.MAX_TX_PUBKEY_LEN]
                    if "b'" == mempool_public_key_b64encoded[:2]:
                        mempool_public_key_b64encoded = transaction[5][2:essentials.MAX_TX_PUBKEY_LEN + 2]

                    last_block = store.tip() or 0
                    # hf2: the operation/openfield caps and the mandatory-message rule are a PRE-FORK admission
                    # policy; the DESTINATION height (tip+1) decides, and the same flag binds the signing scheme.
                    _fork_height = self._hf('fork_height')
                    mempool_post_fork = _fork_height is not None and (last_block + 1) >= _fork_height
                    if mempool_post_fork:
                        mempool_operation = str(transaction[6])
                        mempool_openfield = str(transaction[7])
                    else:
                        if len(transaction[6]) > 30:
                            mempool_result.append("Mempool: Invalid operation len{}".format(len(transaction[6])))
                            continue
                        mempool_operation = str(transaction[6])[:30]
                        if len(transaction[7]) > 100000:
                            mempool_result.append("Mempool: Invalid openfield len{}".format(len(transaction[7])))
                            continue
                        mempool_openfield = str(transaction[7])[:100000]
                        if len(mempool_openfield) <= 4:
                            if mempool_recipient in self.config.mandatory_message.keys():
                                mempool_result.append("Mempool: Missing message - {}".format(
                                    self.config.mandatory_message[mempool_recipient]))
                                continue

                    if mempool_amount_float < 0:
                        mempool_result.append("Mempool: Negative balance spend attempt")
                        continue
                    if mempool_timestamp_float > time_now:
                        mempool_result.append("Mempool: Future transaction rejected {}s".
                                              format(mempool_timestamp_float - time_now))
                        continue
                    if mempool_timestamp_float < time_now - REFUSE_OLDER_THAN:
                        mempool_result.append("Mempool: Too old a transaction")
                        continue

                    try:
                        SignerFactory.verify_tx_signature(
                            mempool_post_fork, mempool_timestamp, mempool_address, mempool_recipient,
                            mempool_amount, mempool_operation, mempool_openfield,
                            mempool_signature_enc, mempool_public_key_b64encoded,
                            registry=self._hf('pk_registry'))
                    except Exception as e:
                        mempool_result.append(f"Mempool: Signature did not match for address ({e})")
                        continue

                    mempool_in = mempool_signature_enc in self._txs
                    # reject transactions already on the confirmed chain
                    ledger_rows = store.signature_rows(mempool_signature_enc, max_height=last_block, limit=1)
                    ledger_in = float(ledger_rows[0][1]) if ledger_rows else None
                    if mempool_in and ledger_in is not None:
                        self._txs.pop(mempool_signature_enc, None)
                        mempool_result.append("Mempool: Transaction deleted from our mempool")
                        continue
                    if ledger_in is not None:
                        mempool_result.append("That transaction is already in our ledger")
                        # a syncing peer: if the tx is not recent, stop taking its mempool for FREEZE_MIN
                        if (peer_ip != '127.0.0.1') and (ledger_in < time_now - 60 * 15):
                            with self.peers_lock:
                                self.peers_sent[peer_ip] = time.time() + FREEZE_MIN * 60
                            self.app_log.warning("Freezing mempool from {} for {} min.".format(peer_ip, FREEZE_MIN))
                        continue
                    if mempool_in:
                        mempool_result.append("That transaction is already in our mempool")
                        continue

                    # balance: confirmed ledger balance minus this address's still-valid pending sends + fees
                    mempool_result.append("Mempool: Received address: {}".format(mempool_address))
                    debit_mempool = DECIMAL0
                    _cutoff = time.time() - REFUSE_OLDER_THAN
                    _pending = [(r[_AMOUNT], r[_OF], r[_OP]) for r in self._txs.values()
                                if r[_ADDR] == mempool_address and float(r[_TS]) > _cutoff]
                    for x in _pending:
                        fee = essentials.fee_calculate(x[1], x[2], last_block, base_fee=self._hf('base_fee'))
                        debit_mempool += quantize_eight(x[0]) + fee

                    balance_pre = store.balance(mempool_address)
                    balance = balance_pre - debit_mempool - quantize_eight(mempool_amount)

                    _fh_bal = self._hf('fork_height')
                    if _fh_bal is not None and (last_block + 1) >= _fh_bal:
                        _mat = essentials.coinbase_maturity(self.config.version == 'regnet')
                        _immature = essentials.immature_coinbase(mempool_address, last_block + 1, store, _mat,
                                                                 self.app_log)
                        balance -= _immature
                        balance_pre -= _immature

                    fee = essentials.fee_calculate(mempool_openfield, mempool_operation, last_block,
                                                   base_fee=self._hf('base_fee'))
                    if quantize_eight(mempool_amount) > balance_pre:
                        mempool_result.append("Mempool: Sending more than owned")
                        continue
                    if balance - fee < 0:
                        mempool_result.append("Mempool: Cannot afford to pay fees")
                        continue

                    self._txs[mempool_signature_enc] = (
                        mempool_timestamp, mempool_address, mempool_recipient, mempool_amount,
                        mempool_signature_enc, mempool_public_key_b64encoded, mempool_operation,
                        mempool_openfield, int(time_now))
                    mempool_result.append("Mempool updated with a received transaction from {}".format(peer_ip))
                    mempool_result.append("Success")  # WARNING: Do not change string or case ever!
                    mempool_size += sys.getsizeof(str(transaction)) / 1000000.0
                return mempool_result
            except Exception as e:
                self.app_log.warning("Mempool: Error processing: {} {}".format(data, e))
                if self.config.debug:
                    exc_type, exc_obj, exc_tb = sys.exc_info()
                    fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                    self.app_log.warning("{} {} {}".format(exc_type, fname, exc_tb.tb_lineno))
                    mempool_result.append("Exception: {}".format(str(e)))
                    raise
        return mempool_result
