"""
Transaction read API (``api_gettransaction*``) methods for ``ApiHandler``, split out of the apihandler god-class as a mixin and
recombined via ``class ApiHandler(TxApiMixin, ...)``. ``__slots__ = ()`` preserves the parent's slot
layout (no per-instance ``__dict__``); the methods are unchanged and run on the composed ApiHandler
instance, so ``self.app_log``/``self.config`` and cross-domain ``self.api_*`` calls resolve via the MRO.
"""
import base64
import amounts
import connections
import essentials
import block_store


def _store(db_handler, node=None):
    """The LMDB ledger (``block_store.BlockStore``) behind a DbHandler (or the node's, as a fallback)."""
    store = getattr(db_handler, "store", None)
    if store is None and node is not None:
        store = getattr(node, "ledger", None)
    return store


def _find_by_txid(store, transaction_id, node=None, recipients=None):
    """The first ledger row whose signature starts with ``transaction_id`` (the legacy txid), or — post-fork —
    whose content txid equals it. ``recipients`` restricts the match to those recipient addresses."""
    def ok(r):
        return recipients is None or r[block_store.RECIP] in recipients
    for r in store.rows_by_sig_prefix(transaction_id):
        if ok(r):
            return r
    fork_height = getattr(node, "fork_height", None) if node is not None else None
    if fork_height is not None:
        try:
            h = store.txid_height(transaction_id)
        except Exception:
            h = None
        if h is not None:
            for r in store.get_block(h) or ():
                if block_store.txid_of(r, fork_height) == transaction_id and ok(r):
                    return r
    return None


def _tx_dict(store, raw, id_key, id_value):
    """The legacy formatted tx dict for one ledger row (with confirmations and the block's miner/time)."""
    transaction = {}
    # current block height, needed for confirmations
    block_height = store.tip()
    transaction[id_key] = id_value
    transaction['time'] = raw[1]
    transaction['hash'] = raw[5]
    transaction['address'] = raw[2]
    transaction['recipient'] = raw[3]
    transaction['amount'] = amounts.display_amount(raw[4])
    transaction['fee'] = amounts.display_amount(raw[8])
    transaction['reward'] = amounts.display_amount(raw[9])
    transaction['operation'] = raw[10]
    transaction['openfield'] = raw[11]
    try:
        transaction['pubkey'] = base64.b64decode(raw[6]).decode('utf-8')
    except:
        transaction['pubkey'] = raw[6]  # support new pubkey schemes
    transaction['blockhash'] = raw[7]
    transaction['blockheight'] = raw[0]
    transaction['confirmations'] = block_height - raw[0]
    # Get more info on the block the tx is in.
    block_data = store.coinbase(raw[0])
    transaction['blocktime'] = block_data[1]
    transaction['blockminer'] = block_data[3]
    return transaction


class TxApiMixin:
    __slots__ = ()

    def api_gettransaction(self, socket_handler, db_handler, peers):
        """
        Returns the full transaction matching a tx id. Takes txid anf format as params (json output if format is True)
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        try:
            # get the txid
            transaction_id = connections.receive(socket_handler)
            # and format
            format = connections.receive(socket_handler)
            # raw tx details
            store = _store(db_handler, getattr(self, "node", None))
            raw = _find_by_txid(store, transaction_id, getattr(self, "node", None))
            if not format:
                connections.send(socket_handler, raw)
                print('api_gettransaction', format, raw)
                return

            transaction = _tx_dict(store, raw, 'txid', transaction_id)
            print('api_gettransaction', format, transaction)
            connections.send(socket_handler, transaction)
        except Exception as e:
            # self.app_log.warning(e)
            raise

    def api_gettransactionbysignature(self, socket_handler, db_handler, peers):
        """
        Returns the full transaction matching a signature. Takes signature and format as params (json output if format is True)
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        try:
            # get the txid
            signature = connections.receive(socket_handler)
            # and format
            format = connections.receive(socket_handler)
            # raw tx details
            store = _store(db_handler, getattr(self, "node", None))
            rows = store.signature_rows(signature, limit=1)
            raw = rows[0] if rows else None
            if not format:
                connections.send(socket_handler, raw)
                print('api_gettransactionbysignature', format, raw)
                return

            transaction = _tx_dict(store, raw, 'signature', signature)
            print('api_gettransactionbysignature', format, transaction)
            connections.send(socket_handler, transaction)
        except Exception as e:
            # self.app_log.warning(e)
            raise

    def api_gettransaction_for_recipients(self, socket_handler, db_handler, peers):
            """
            Warning: this is currently very slow
            Returns the full transaction matching a tx id for a list of recipient addresses.
            Takes txid and format as params (json output if format is True)
            :param socket_handler:
            :param db_handler:
            :param peers:
            :return:
            """
            try:
                # get the txid
                transaction_id = connections.receive(socket_handler)
                # then the recipient list
                addresses = connections.receive(socket_handler)
                # and format
                format = connections.receive(socket_handler)
                # Validate the recipient list: it must be a bounded list of well-formed addresses.
                if (not isinstance(addresses, (list, tuple)) or not addresses
                        or len(addresses) > 500
                        or not all(isinstance(a, str) and essentials.address_validate(a) for a in addresses)):
                    connections.send(socket_handler, None)
                    return
                # raw tx details
                store = _store(db_handler, getattr(self, "node", None))
                raw = _find_by_txid(store, transaction_id, getattr(self, "node", None), recipients=set(addresses))
                if not format:
                    connections.send(socket_handler, raw)
                    print('api_gettransaction_for_recipients', format, raw)
                    return

                transaction = _tx_dict(store, raw, 'txid', transaction_id)
                print('api_gettransaction_for_recipients', format, transaction)
                connections.send(socket_handler, transaction)
            except Exception as e:
                # self.app_log.warning(e)
                raise
