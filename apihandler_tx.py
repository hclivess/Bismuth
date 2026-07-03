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
        transaction = {}
        try:
            # get the txid
            transaction_id = connections.receive(socket_handler)
            # and format
            format = connections.receive(socket_handler)
            # raw tx details
            if self.config.old_sqlite:
                db_handler.execute_param(db_handler.h,
                                         "SELECT * FROM transactions WHERE signature like ?1",
                                         (transaction_id + '%',))
            else:
                db_handler.execute_param(db_handler.h,
                                        "SELECT * FROM transactions WHERE substr(signature,1,4)=substr(?1,1,4) and  signature like ?1",
                                        (transaction_id+'%',))
            raw = db_handler.h.fetchone()
            if not format:
                connections.send(socket_handler, raw)
                print('api_gettransaction', format, raw)
                return

            # current block height, needed for confirmations #
            db_handler.execute(db_handler.h, "SELECT MAX(block_height) FROM transactions")
            block_height = db_handler.h.fetchone()[0]
            transaction['txid'] = transaction_id
            transaction['time'] = raw[1]
            transaction['hash'] = raw[5]
            transaction['address'] = raw[2]
            transaction['recipient'] = raw[3]
            transaction['amount'] = amounts.display_amount(raw[4])
            transaction['fee'] = amounts.display_amount(raw[8])
            transaction['reward'] = amounts.display_amount(raw[9])
            transaction['operation']= raw[10]
            transaction['openfield'] = raw[11]
            try:
                transaction['pubkey'] = base64.b64decode(raw[6]).decode('utf-8')
            except:
                transaction['pubkey'] = raw[6]  # support new pubkey schemes
            transaction['blockhash'] = raw[7]
            transaction['blockheight'] = raw[0]
            transaction['confirmations'] = block_height - raw[0]
            # Get more info on the block the tx is in.
            db_handler.execute_param(db_handler.h,
                                    "SELECT timestamp, recipient FROM transactions WHERE block_height= ? AND reward > 0",
                                    (raw[0],))
            block_data = db_handler.h.fetchone()
            transaction['blocktime'] = block_data[0]
            transaction['blockminer'] = block_data[1]
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
        transaction = {}
        try:
            # get the txid
            signature = connections.receive(socket_handler)
            # and format
            format = connections.receive(socket_handler)
            # raw tx details
            if self.config.old_sqlite:
                db_handler.execute_param(db_handler.h,
                                         "SELECT * FROM transactions WHERE signature = ?1",
                                         (signature,))
            else:
                db_handler.execute_param(db_handler.h,
                                         "SELECT * FROM transactions WHERE substr(signature,1,4)=substr(?1,1,4) and  signature = ?1",
                                         (signature,))
            raw = db_handler.h.fetchone()
            if not format:
                connections.send(socket_handler, raw)
                print('api_gettransactionbysignature', format, raw)
                return

            # current block height, needed for confirmations
            db_handler.execute(db_handler.h, "SELECT MAX(block_height) FROM transactions")
            block_height = db_handler.h.fetchone()[0]
            transaction['signature'] = signature
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
            db_handler.execute_param(db_handler.h,
                                    "SELECT timestamp, recipient FROM transactions WHERE block_height= ? AND reward > 0",
                                    (raw[0],))
            block_data = db_handler.h.fetchone()
            transaction['blocktime'] = block_data[0]
            transaction['blockminer'] = block_data[1]
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
            transaction = {}
            try:
                # get the txid
                transaction_id = connections.receive(socket_handler)
                # then the recipient list
                addresses = connections.receive(socket_handler)
                # and format
                format = connections.receive(socket_handler)
                # Validate the recipient list: it must be a bounded list of well-formed addresses.
                # NEVER interpolate received data into SQL text; the IN(...) list is bound with
                # placeholders below so a crafted address element cannot break out of the string.
                if (not isinstance(addresses, (list, tuple)) or not addresses
                        or len(addresses) > 500
                        or not all(isinstance(a, str) and essentials.address_validate(a) for a in addresses)):
                    connections.send(socket_handler, None)
                    return
                placeholders = ",".join("?" * len(addresses))
                sig_like = transaction_id + '%'
                # raw tx details
                if self.config.old_sqlite:
                    db_handler.execute_param(db_handler.h,
                                            "SELECT * FROM transactions WHERE recipient IN ({}) AND signature LIKE ?".format(placeholders),
                                            (*addresses, sig_like))
                else:
                    db_handler.execute_param(db_handler.h,
                                             "SELECT * FROM transactions WHERE recipient IN ({}) AND substr(signature,1,4)=substr(?,1,4) and signature LIKE ?".format(
                                                 placeholders),
                                             (*addresses, sig_like, sig_like))

                raw = db_handler.h.fetchone()
                if not format:
                    connections.send(socket_handler, raw)
                    print('api_gettransaction_for_recipients', format, raw)
                    return

                # current block height, needed for confirmations #
                db_handler.execute(db_handler.h, "SELECT MAX(block_height) FROM transactions")
                block_height = db_handler.h.fetchone()[0]
                transaction['txid'] = transaction_id
                transaction['time'] = raw[1]
                transaction['hash'] = raw[5]
                transaction['address'] = raw[2]
                transaction['recipient'] = raw[3]
                transaction['amount'] = amounts.display_amount(raw[4])
                transaction['fee'] = amounts.display_amount(raw[8])
                transaction['reward'] = amounts.display_amount(raw[9])
                transaction['operation']= raw[10]
                transaction['openfield'] = raw[11]

                try:
                    transaction['pubkey'] = base64.b64decode(raw[6]).decode('utf-8')
                except:
                    transaction['pubkey'] = raw[6]  # support new pubkey schemes

                transaction['blockhash'] = raw[7]
                transaction['blockheight'] = raw[0]
                transaction['confirmations'] = block_height - raw[0]
                # Get more info on the block the tx is in.
                db_handler.execute_param(db_handler.h,
                                        "SELECT timestamp, recipient FROM transactions WHERE block_height= ? AND reward > 0",
                                        (raw[0],))
                block_data = db_handler.h.fetchone()
                transaction['blocktime'] = block_data[0]
                transaction['blockminer'] = block_data[1]
                print('api_gettransaction_for_recipients', format, transaction)
                connections.send(socket_handler, transaction)
            except Exception as e:
                # self.app_log.warning(e)
                raise
