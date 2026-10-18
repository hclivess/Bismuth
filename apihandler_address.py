"""
Address & balance read API (``api_getaddress*``, ``api_*balance``, ``api_*received``) methods for ``ApiHandler``, split out of the apihandler god-class as a mixin and
recombined via ``class ApiHandler(AddressApiMixin, ...)``. ``__slots__ = ()`` preserves the parent's slot
layout (no per-instance ``__dict__``); the methods are unchanged and run on the composed ApiHandler
instance, so ``self.app_log``/``self.config`` and cross-domain ``self.api_*`` calls resolve via the MRO.
"""
import base64
import amounts
import block_format
import connections
import block_store
from polysign.signerfactory import SignerFactory


def _store(db_handler, node=None):
    """The LMDB ledger (``block_store.BlockStore``) behind a DbHandler (or the node's, as a fallback)."""
    store = getattr(db_handler, "store", None)
    if store is None and node is not None:
        store = getattr(node, "ledger", None)
    return store


def _parts_upto(store, address, max_block_height):
    """(credited amount, rewards, debited amount, fees) in integer units counting only blocks
    <= ``max_block_height``: the maintained balance entry minus the address's rows above that height."""
    parts = list(store.balance_parts(address))
    tip = store.tip()
    if tip is None or max_block_height < 1:
        return 0, 0, 0, 0
    if max_block_height < tip:
        for r in store.address_rows(address, lo=int(max_block_height) + 1):
            if str(r[block_store.RECIP]) == address:
                parts[0] -= block_store.units(r[block_store.AMOUNT])
                parts[1] -= block_store.units(r[block_store.REWARD])
            if str(r[block_store.ADDR]) == address:
                parts[2] -= block_store.units(r[block_store.AMOUNT])
                parts[3] -= block_store.units(r[block_store.FEE])
    return tuple(parts)


class AddressApiMixin:
    __slots__ = ()

    def api_getaddressinfo(self, socket_handler, db_handler, peers):
        """
        Returns a dict with
        known: Did that address appear on a transaction?
        pubkey: The pubkey of the address if it signed a transaction,
        :param address: The bismuth address to examine
        :return: dict
        """
        info = {'known': False, 'pubkey':''}
        # get the address
        address = connections.receive(socket_handler)
        # print('api_getaddressinfo', address)
        try:
            # format check
            if not SignerFactory.address_is_valid(address):
                self.app_log.info("Bad address format <{}>".format(address))
                connections.send(socket_handler, info)
                return
            try:
                store = _store(db_handler, getattr(self, "node", None))
                if store.address_rows(address, reverse=False, limit=1):
                    # at least one known tx
                    info['known'] = True
                    try:
                        pubkey = store.first_pubkey(address)
                        if pubkey is None:
                            raise ValueError(f"no public key on the ledger for {address}")
                        info['pubkey'] = base64.b64decode(pubkey).decode('utf-8')
                    except Exception as e:
                        self.app_log.warning(e)

            except Exception as e:
                self.app_log.warning(e)

            # returns info
            # print("info", info)
            connections.send(socket_handler, info)
        except Exception as e:
            self.app_log.warning(e)

    def api_getaddressrange(self, socket_handler, db_handler, peers):
        """
        Returns a given number of transactions, maximum of 500 entries. Ignores blocks where no transactions of a given address happened.
        Reorganizes parameters to a quickly accessible json.
        Unnecessary data are removed.

        :param socket_handler:
        :param db_handler: (UNUSED)
        :param peers: (UNUSED)
        :return:
        """

        address = connections.receive(socket_handler)
        starting_block = connections.receive(socket_handler)
        limit = connections.receive(socket_handler)

        # Coerce and clamp both client-supplied bounds: limit to [0, 500] (a negative limit must never mean
        # "unbounded"); a non-int/negative starting_block would either error or scan the whole history.
        try:
            limit = min(500, max(0, int(limit)))
            starting_block = max(0, int(starting_block))
        except (TypeError, ValueError):
            connections.send(socket_handler, [])
            return

        store = _store(db_handler, getattr(self, "node", None))
        result = store.address_rows(address, reverse=False, lo=starting_block, limit=limit) if limit else []
        blocks = block_format.blockstojson(result)
        connections.send(socket_handler, blocks)

    def api_getaddresssince(self, socket_handler, db_handler, peers):
        """
        Returns the full transactions following a given block_height (will not include the given height) for the given address, with at least min_confirmations confirmations,
        as well as last considered block.
        Returns at most transactions from 720 blocks at a time (the most *older* ones if it truncates) so about 12 hours worth of data.

        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        info = []
        # get the last known block
        since_height = int(connections.receive(socket_handler))
        min_confirmations = int(connections.receive(socket_handler))
        address = str(connections.receive(socket_handler))
        print('api_getaddresssince', since_height, min_confirmations, address)
        try:
            try:
                store = _store(db_handler, getattr(self, "node", None))
                # what is the max block height to consider ?
                block_height = min(store.tip() - min_confirmations, since_height+720)
                rows = (store.address_rows(address, reverse=False, lo=since_height + 1, hi=block_height)
                        if block_height > since_height else [])
                info = [amounts.display_row(r) for r in rows]
            except Exception as e:
                print("Exception api_getaddresssince: {}".format(e))
                raise
            connections.send(socket_handler, {'last': block_height, 'minconf': min_confirmations, 'transactions': info})
        except Exception as e:
            # self.app_log.warning(e)
            raise

    def _get_balance(self, db_handler, address, minconf=1):
        """
        Queries the db to get the balance of a single address
        :param address:
        :param minconf:
        :return:
        """
        try:
            store = _store(db_handler, getattr(self, "node", None))
            # what is the max block height to consider ?
            max_block_height = store.tip() - minconf
            # calc balance up to this block_height: credit (amount + reward) - debit (amount + fee)
            credited, rewards, debited, fees = _parts_upto(store, address, max_block_height)
            # keep as float
            balance = float(amounts.to_decimal(credited + rewards - debited - fees))
        except Exception as e:
            # self.app_log.warning(e)
            raise
        return balance

    def api_getbalance(self, socket_handler, db_handler, peers):
        """
        returns total balance for a list of addresses and minconf
        BEWARE: this is NOT the json rpc getbalance (that get balance for an account, not an address)
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        balance = 0
        try:
            # get the addresses (it's a list, even if a single address)
            addresses = connections.receive(socket_handler)
            minconf = connections.receive(socket_handler)
            if minconf < 1:
                minconf = 1
            # TODO: read the tip once for all the addresses?
            for address in addresses:
                balance += self._get_balance(db_handler, address, minconf)
            # print('api_getbalance', addresses, minconf,':', balance)
            connections.send(socket_handler, balance)
        except Exception as e:
            raise

    def _get_received(self, db_handler, address, minconf=1):
        """
        Queries the db to get the total received amount of a single address
        :param address:
        :param minconf:
        :return:
        """
        try:
            # TODO : for this one and _get_balance, request max block height out of the loop and pass it as a param to alleviate db load
            store = _store(db_handler, getattr(self, "node", None))
            # what is the max block height to consider ?
            max_block_height = store.tip() - minconf
            # calc received up to this block_height
            credit = float(amounts.to_decimal(_parts_upto(store, address, max_block_height)[0]))
        except Exception as e:
            # self.app_log.warning(e)
            raise
        return credit

    def api_getreceived(self, socket_handler, db_handler, peers):
        """
        returns total received amount for a *list* of addresses and minconf
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        received = 0
        try:
            # get the addresses (it's a list, even if a single address)
            addresses = connections.receive(socket_handler)
            minconf = connections.receive(socket_handler)
            if minconf < 1:
                minconf = 1
            # TODO: read the tip once for all the addresses?
            for address in addresses:
                received += self._get_received(db_handler, address, minconf)
            print('api_getreceived', addresses, minconf,':', received)
            connections.send(socket_handler, received)
        except Exception as e:
            # self.app_log.warning(e)
            raise

    def api_listreceived(self, socket_handler, db_handler, peers):
        """
        Returns the total amount received for each given address with minconf, including empty addresses or not.
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        received = {}
        # TODO: this is temporary.
        # Will need more work to send full featured info needed for https://bitcoin.org/en/developer-reference#listreceivedbyaddress
        # (confirmations and tx list)
        try:
            # get the addresses (it's a list, even if a single address)
            addresses = connections.receive(socket_handler)
            minconf = connections.receive(socket_handler)
            if minconf < 1:
                minconf = 1
            include_empty = connections.receive(socket_handler)
            for address in addresses:
                temp = self._get_received(db_handler, address, minconf)
                if include_empty or temp >0:
                    received[address] = temp
            print('api_listreceived', addresses, minconf,':', received)
            connections.send(socket_handler, received)
        except Exception as e:
            # self.app_log.warning(e)
            raise

    def api_listbalance(self, socket_handler, db_handler, peers):
        """
        Returns the total amount received for each given address with minconf, including empty addresses or not.
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        balances = {}
        try:
            # get the addresses (it's a list, even if a single address)
            addresses = connections.receive(socket_handler)
            minconf = connections.receive(socket_handler)
            if minconf < 1:
                minconf = 1
            include_empty = connections.receive(socket_handler)
            # TODO: read the tip once for all the addresses?
            for address in addresses:
                temp = self._get_balance(db_handler, address, minconf)
                if include_empty or temp >0:
                    balances[address] = temp
            print('api_listbalance', addresses, minconf,':', balances)
            connections.send(socket_handler, balances)
        except Exception as e:
            # self.app_log.warning(e)
            raise
