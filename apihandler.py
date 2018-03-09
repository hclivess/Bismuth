"""
API Command handler module for Bismuth nodes
@EggPoolNet
Needed for Json-RPC server or other third party interaction
"""

import re
import sqlite3
import base64
# modular handlers will need access to the database methods under some form, so it needs to be modular too.
# Here, I just duplicated the minimum needed code from node, further refactoring with classes will follow.
import dbhandler, connections, peershandler

__version__ = "0.0.2"


class ApiHandler:
    """
    The API commands manager. Extra commands, not needed for node communication, but for third party tools.
    Handles all commands prefixed by "api_".
    It's called from client threads, so it has to be thread safe.
    """

    __slots__ = ('app_log','config')

    def __init__(self, app_log, config=None):
        self.app_log = app_log
        self.config = config

    def dispatch(self, method, socket_handler, ledger_db, peers):
        """
        Routes the call to the right method
        :return:
        """
        # Easier to ask forgiveness than ask permission
        try:
            """
            All API methods share the same interface. Not storing in properties since it has to be thread safe.
            This is not pretty, this will evolve with more modular code.
            Primary goal is to limit the changes in node.py code and allow more flexibility in this class, like some plugin.
            """
            result = getattr(self, method)(socket_handler, ledger_db, peers)
            return result
        except AttributeError:
            print('KO')
            self.app_log.warning("API Method <{}> does not exist.".format(method))
            return False

    def api_ping(self, socket_handler, ledger_db, peers):
        """
        Void, just to allow the client to keep the socket open (avoids timeout)
        :param socket_handler:
        :param ledger_db:
        :param peers:
        :return: 'api_pong'
        """
        connections.send(socket_handler, 'api_pong')

    def api_getaddressinfo(self, socket_handler, ledger_db, peers):
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
            if not re.match('[abcdef0123456789]{56}', address):
                self.app_log.info("Bad address format <{}>".format(address))
                connections.send(socket_handler, info)
                return
            try:
                dbhandler.execute_param(self.app_log, ledger_db,
                                        ('SELECT block_height FROM transactions WHERE address= ? or recipient= ? LIMIT 1;'),
                                        (address,address))
                ledger_db.fetchone()[0]
                # no exception? then we have at least one known tx
                info['known'] = True
                dbhandler.execute_param(self.app_log, ledger_db, ('SELECT public_key FROM transactions WHERE address= ? and reward = 0 LIMIT 1;'), (address,))
                try:
                    info['pubkey'] = ledger_db.fetchone()[0]
                    info['pubkey'] = base64.b64decode(info['pubkey']).decode('utf-8')
                except Exception as e:
                    print(e)
                    pass
            except Exception as e:
                pass
            # returns info
            # print("info", info)
            connections.send(socket_handler, info)
        except Exception as e:
            pass

    def _get_balance(self, ledger_db, address, minconf=1):
        """
        Queries the db to get the balance of a single address
        :param address:
        :param minconf:
        :return:
        """
        try:
            dbhandler.execute(self.app_log, ledger_db, "SELECT MAX(block_height) FROM transactions")
            # what is the max block height to consider ?
            max_block_height = ledger_db.fetchone()[0] - minconf
            # calc balance up to this block_height
            dbhandler.execute_param(self.app_log, ledger_db, "SELECT sum(amount) FROM transactions WHERE recipient = ? and block_height <= ?;", (address, max_block_height))
            credit = ledger_db.fetchone()[0]
            if not credit:
                credit = 0
            # debits + fee - reward
            dbhandler.execute_param(self.app_log, ledger_db, "SELECT sum(amount)+sum(fee)-sum(reward) FROM transactions WHERE address = ? and block_height <= ?;", (address, max_block_height))
            debit = ledger_db.fetchone()[0]
            if not debit:
                debit = 0
            # keep as float
            #balance = '{:.8f}'.format(credit - debit)
            balance = credit - debit
        except Exception as e:
            print(e)
            raise
        return balance

    def api_getbalance(self, socket_handler, ledger_db, peers):
        """
        returns total balance for a list of addresses and minconf
        BEWARE: this is NOT the json rpc getbalance (that get balance for an account, not an address)
        :param socket_handler:
        :param ledger_db:
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
            # TODO: Better to use a single sql query with all addresses listed?
            for address in addresses:
                balance += self._get_balance(ledger_db, address, minconf)
            #print('api_getbalance', addresses, minconf,':', balance)
            connections.send(socket_handler, balance)
        except Exception as e:
            raise

    def _get_received(self, ledger_db, address, minconf=1):
        """
        Queries the db to get the total received amount of a single address
        :param address:
        :param minconf:
        :return:
        """
        try:
            # TODO : for this one and _get_balance, request max block height out of the loop and pass it as a param to alleviate db load
            dbhandler.execute(self.app_log, ledger_db, "SELECT MAX(block_height) FROM transactions")
            # what is the max block height to consider ?
            max_block_height = ledger_db.fetchone()[0] - minconf
            # calc received up to this block_height
            dbhandler.execute_param(self.app_log, ledger_db, "SELECT sum(amount) FROM transactions WHERE recipient = ? and block_height <= ?;", (address, max_block_height))
            credit = ledger_db.fetchone()[0]
            if not credit:
                credit = 0
        except Exception as e:
            print(e)
            raise
        return credit

    def api_getreceived(self, socket_handler, ledger_db, peers):
        """
        returns total received amount for a *list* of addresses and minconf
        :param socket_handler:
        :param ledger_db:
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
            # TODO: Better to use a single sql query with all addresses listed?
            for address in addresses:
                received += self._get_received(ledger_db, address, minconf)
            print('api_getreceived', addresses, minconf,':', received)
            connections.send(socket_handler, received)
        except Exception as e:
            raise

    def api_listreceived(self, socket_handler, ledger_db, peers):
        """
        Returns the total amount received for each given address with minconf, including empty addresses or not.
        :param socket_handler:
        :param ledger_db:
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
                temp = self._get_received(ledger_db, address, minconf)
                if include_empty or temp >0:
                    received[address] = temp
            print('api_listreceived', addresses, minconf,':', received)
            connections.send(socket_handler, received)
        except Exception as e:
            raise

    def api_listbalance(self, socket_handler, ledger_db, peers):
        """
        Returns the total amount received for each given address with minconf, including empty addresses or not.
        :param socket_handler:
        :param ledger_db:
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
            # TODO: Better to use a single sql query with all addresses listed?
            for address in addresses:
                temp = self._get_balance(ledger_db, address, minconf)
                if include_empty or temp >0:
                    balances[address] = temp
            print('api_listbalance', addresses, minconf,':', balances)
            connections.send(socket_handler, balances)
        except Exception as e:
            raise

    def api_gettransaction(self, socket_handler, ledger_db, peers):
        """
        returns total balance for a list of addresses and minconf
        BEWARE: this is NOT the json rpc getbalance (that get balance for an account, not an address)
        :param socket_handler:
        :param ledger_db:
        :param peers:
        :return:
        """
        transaction = {}
        try:
            # get the txid
            transaction_id = connections.receive(socket_handler)
            # and format
            format = connections.receive(socket_handler)
            # raw tx details
            dbhandler.execute_param(self.app_log, ledger_db,
                                    "SELECT * FROM transactions WHERE signature like ?",
                                    (transaction_id+'%',))
            raw = ledger_db.fetchone()
            if not format:
                connections.send(socket_handler, raw)
                print('api_gettransaction', format, raw)
                return

            # current block height, needed for confirmations #
            dbhandler.execute(self.app_log, ledger_db, "SELECT MAX(block_height) FROM transactions")
            block_height = ledger_db.fetchone()[0]
            transaction['txid'] = transaction_id
            transaction['time'] = raw[1]
            transaction['hash'] = raw[5]
            transaction['address'] = raw[2]
            transaction['recipient'] = raw[3]
            transaction['amount'] = raw[4]
            transaction['fee'] = raw[8]
            transaction['reward'] = raw[9]
            transaction['keep'] = raw[10]
            transaction['openfield'] = raw[11]
            transaction['pubkey'] = base64.b64decode(raw[6]).decode('utf-8')
            transaction['blockhash'] = raw[7]
            transaction['blockheight'] = raw[0]
            transaction['confirmations'] = block_height - raw[0]
            # Get more info on the block the tx is in.
            dbhandler.execute_param(self.app_log, ledger_db,
                                    "SELECT timestamp, recipient FROM transactions WHERE block_height= ? AND reward > 0",
                                    (raw[0],))
            block_data = ledger_db.fetchone()
            transaction['blocktime'] = block_data[0]
            transaction['blockminer'] = block_data[1]
            print('api_gettransaction', format, transaction)
            connections.send(socket_handler, transaction)
        except Exception as e:
            raise

    def api_getpeerinfo(self, socket_handler, ledger_db, peers):
        """
        Returns a list of connected peers
        See https://bitcoin.org/en/developer-reference#getpeerinfo
        To be adjusted
        :return: list(dict)
        """
        print('api_getpeerinfo')
        # TODO: Get what we can from peers, more will come when connections and connection stats will be modular, too.
        try:
            info = [{'id':id, 'addr':ip, 'inbound': True} for id, ip in enumerate(peers.consensus)]
            # TODO: peers will keep track of extra info, like port, last time, block_height aso.
            # TODO: add outbound connection
            connections.send(socket_handler, info)
        except Exception as e:
            pass


