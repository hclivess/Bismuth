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
import connections, peershandler
import threading
import os, sys
#import math
import mempool as mp
import json

__version__ = "0.0.5"


class ApiHandler:
    """
    The API commands manager. Extra commands, not needed for node communication, but for third party tools.
    Handles all commands prefixed by "api_".
    It's called from client threads, so it has to be thread safe.
    """

    __slots__ = ('app_log', 'config', 'callback_lock', 'callbacks')

    def __init__(self, app_log, config=None):
        self.app_log = app_log
        self.config = config
        # Avoid mixing answers to commands with callbacks
        self.callback_lock = threading.Lock()
        # list of sockets that asked for a callback (new block notification)
        # Not used yet.
        self.callbacks = []

    def dispatch(self, method, socket_handler, db_handler, peers):
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
            result = getattr(self, method)(socket_handler, db_handler, peers)
            return result
        except AttributeError:
            raise
            print('KO')
            self.app_log.warning("API Method <{}> does not exist.".format(method))
            return False

    def api_mempool(self, socket_handler, db_handler, peers):
        """
        Returns all the TX from mempool
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return: list of mempool tx
        """
        txs = mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND)
        connections.send(socket_handler, txs)

    def api_clearmempool(self, socket_handler, db_handler, peers):
        """
        Empty the current mempool
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return: 'ok'
        """
        mp.MEMPOOL.clear()
        connections.send(socket_handler, 'ok')        
        
    def api_ping(self, socket_handler, db_handler, peers):
        """
        Void, just to allow the client to keep the socket open (avoids timeout)
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return: 'api_pong'
        """
        connections.send(socket_handler, 'api_pong')

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
            if not re.match('[abcdef0123456789]{56}', address):
                self.app_log.info("Bad address format <{}>".format(address))
                connections.send(socket_handler, info)
                return
            try:
                db_handler.execute_param(db_handler.h3,
                                        ('SELECT block_height FROM transactions WHERE address= ? or recipient= ? LIMIT 1;'),
                                        (address,address))
                _ = db_handler.h3.fetchone()[0]
                # no exception? then we have at least one known tx
                info['known'] = True
                db_handler.execute_param(db_handler.h3, ('SELECT public_key FROM transactions WHERE address= ? and reward = 0 LIMIT 1;'), (address,))
                try:
                    info['pubkey'] = db_handler.h3.fetchone()[0]
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

    def api_getblocksince(self, socket_handler, db_handler, peers):
        """
        Returns the full blocks and transactions following a given block_height
        Returns at most transactions from 10 blocks (the most recent ones if it truncates)
        Used by the json-rpc server to poll and be notified of tx and new blocks.
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        info = []
        # get the last known block
        since_height = connections.receive(socket_handler)
        #print('api_getblocksince', since_height)
        try:
            try:
                db_handler.execute(db_handler.h3, "SELECT MAX(block_height) FROM transactions")
                # what is the min block height to consider ?
                block_height = max(db_handler.h3.fetchone()[0]-11, since_height)
                #print("block_height",block_height)
                db_handler.execute_param(db_handler.h3,
                                        ('SELECT * FROM transactions WHERE block_height > ?;'),
                                        (block_height, ))
                info = db_handler.db_handler.h3.fetchall()
                # it's a list of tuples, send as is.
                #print(all)
            except Exception as e:
                print(e)
                raise
            # print("info", info)
            connections.send(socket_handler, info)
        except Exception as e:
            print(e)
            raise

    def api_getblockswhereoflike(self, socket_handler, db_handler, peers):
        """
        Returns the full transactions following a given block_height and with openfield begining by the given string
        Returns at most transactions from 1440 blocks at a time (the most *older* ones if it truncates) so about 1 day worth of data.
        Maybe huge, use with caution and on restrictive queries only.
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        info = []
        # get the last known block
        since_height = int(connections.receive(socket_handler))
        where_openfield_like = connections.receive(socket_handler)+'%'
        #print('api_getblockswhereoflike', since_height, where_openfield_like)
        try:
            try:
                db_handler.execute(db_handler.h3, "SELECT MAX(block_height) FROM transactions")
                # what is the max block height to consider ?
                block_height = min(db_handler.h3.fetchone()[0], since_height+1440)
                #print("block_height", since_height, block_height)
                db_handler.execute_param(db_handler.h3,
                                        'SELECT * FROM transactions WHERE block_height > ? and block_height <= ? and openfield like ?',
                                        (since_height, block_height, where_openfield_like) )
                info = db_handler.db_handler.h3.fetchall()
                # it's a list of tuples, send as is.
                #print("info", info)
            except Exception as e:
                print("error", e)
                raise
            # Add the last fetched block so the client will be able to fetch the next block
            info.append([block_height])
            connections.send(socket_handler, info)
        except Exception as e:
            print(e)
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            raise

    def api_getblocksincewhere(self, socket_handler, db_handler, peers):
        """
        Returns the full transactions following a given block_height and with specific conditions
        Returns at most transactions from 720 blocks at a time (the most *older* ones if it truncates) so about 12 hours worth of data.
        Maybe huge, use with caution and restrictive queries only.
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        info = []
        # get the last known block
        since_height = connections.receive(socket_handler)
        where_conditions = connections.receive(socket_handler)
        print('api_getblocksincewhere', since_height, where_conditions)
        # TODO: feed as array to have a real control and avoid sql injection !important
        # Do *NOT* use in production until it's done.
        raise ValueError("Unsafe, do not use yet")
        """
        [
        ['','openfield','like','egg%']
        ]

        [
        ['', '('],
        ['','reward','>','0']
        ['and','recipient','in',['','','']]
        ['', ')'],
        ]
        """
        where_assembled = where_conditions
        conditions_assembled = ()
        try:
            try:
                db_handler.execute(db_handler.h3, "SELECT MAX(block_height) FROM transactions")
                # what is the max block height to consider ?
                block_height = min(db_handler.h3.fetchone()[0], since_height+720)
                #print("block_height",block_height)
                db_handler.execute_param(db_handler.h3,
                                        ('SELECT * FROM transactions WHERE block_height > ? and block_height <= ? and ( '+where_assembled+')'),
                                        (since_height, block_height)+conditions_assembled)
                info = db_handler.db_handler.h3.fetchall()
                # it's a list of tuples, send as is.
                #print(all)
            except Exception as e:
                print(e)
                raise
            # print("info", info)
            connections.send(socket_handler, info)
        except Exception as e:
            print(e)
            raise
            
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
                db_handler.execute(db_handler.h3, "SELECT MAX(block_height) FROM transactions")
                # what is the max block height to consider ?
                block_height = min(db_handler.h3.fetchone()[0] - min_confirmations, since_height+720)
                db_handler.execute_param(db_handler.h3,
                                        ('SELECT * FROM transactions WHERE block_height > ? AND block_height <= ? '
                                         'AND ((address = ?) OR (recipient = ?)) ORDER BY block_height ASC'),
                                        (since_height, block_height, address, address))
                info = db_handler.db_handler.h3.fetchall()
            except Exception as e:
                print("Exception api_getaddresssince:".format(e))
                raise
            connections.send(socket_handler, {'last': block_height, 'minconf': min_confirmations, 'transactions': info})
        except Exception as e:
            print(e)
            raise       

    def _get_balance(self, db_handler, address, minconf=1):
        """
        Queries the db to get the balance of a single address
        :param address:
        :param minconf:
        :return:
        """
        try:
            db_handler.execute(db_handler.h3, "SELECT MAX(block_height) FROM transactions")
            # what is the max block height to consider ?
            max_block_height = db_handler.h3.fetchone()[0] - minconf
            # calc balance up to this block_height
            db_handler.execute_param(db_handler.h3, "SELECT sum(amount)+sum(reward) FROM transactions WHERE recipient = ? and block_height <= ?;", (address, max_block_height))
            credit = db_handler.h3.fetchone()[0]
            if not credit:
                credit = 0
            # debits + fee - reward
            db_handler.execute_param(db_handler.h3, "SELECT sum(amount)+sum(fee) FROM transactions WHERE address = ? and block_height <= ?;", (address, max_block_height))
            debit = db_handler.h3.fetchone()[0]
            if not debit:
                debit = 0
            # keep as float
            #balance = '{:.8f}'.format(credit - debit)
            balance = credit - debit
        except Exception as e:
            print(e)
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
            # TODO: Better to use a single sql query with all addresses listed?
            for address in addresses:
                balance += self._get_balance(db_handler, address, minconf)
            #print('api_getbalance', addresses, minconf,':', balance)
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
            db_handler.execute(db_handler.h3, "SELECT MAX(block_height) FROM transactions")
            # what is the max block height to consider ?
            max_block_height = db_handler.h3.fetchone()[0] - minconf
            # calc received up to this block_height
            db_handler.execute_param(db_handler.h3, "SELECT sum(amount) FROM transactions WHERE recipient = ? and block_height <= ?;", (address, max_block_height))
            credit = db_handler.h3.fetchone()[0]
            if not credit:
                credit = 0
        except Exception as e:
            print(e)
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
            # TODO: Better to use a single sql query with all addresses listed?
            for address in addresses:
                received += self._get_received(db_handler, address, minconf)
            print('api_getreceived', addresses, minconf,':', received)
            connections.send(socket_handler, received)
        except Exception as e:
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
            # TODO: Better to use a single sql query with all addresses listed?
            for address in addresses:
                temp = self._get_balance(db_handler, address, minconf)
                if include_empty or temp >0:
                    balances[address] = temp
            print('api_listbalance', addresses, minconf,':', balances)
            connections.send(socket_handler, balances)
        except Exception as e:
            raise

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
            # and format
            format = connections.receive(socket_handler)
            # raw tx details
            db_handler.execute_param(db_handler.h3,
                                    "SELECT * FROM transactions WHERE signature like ?",
                                    (transaction_id+'%',))
            raw = db_handler.h3.fetchone()
            if not format:
                connections.send(socket_handler, raw)
                print('api_gettransaction', format, raw)
                return

            # current block height, needed for confirmations #
            db_handler.execute(db_handler.h3, "SELECT MAX(block_height) FROM transactions")
            block_height = db_handler.h3.fetchone()[0]
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
            db_handler.execute_param(db_handler.h3,
                                    "SELECT timestamp, recipient FROM transactions WHERE block_height= ? AND reward > 0",
                                    (raw[0],))
            block_data = db_handler.h3.fetchone()
            transaction['blocktime'] = block_data[0]
            transaction['blockminer'] = block_data[1]
            print('api_gettransaction', format, transaction)
            connections.send(socket_handler, transaction)
        except Exception as e:
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
            db_handler.execute_param(db_handler.h3,
                                    "SELECT * FROM transactions WHERE signature = ?",
                                    (signature,))
            raw = db_handler.h3.fetchone()
            if not format:
                connections.send(socket_handler, raw)
                print('api_gettransactionbysignature', format, raw)
                return

            # current block height, needed for confirmations #
            db_handler.execute(db_handler.h3, "SELECT MAX(block_height) FROM transactions")
            block_height = db_handler.h3.fetchone()[0]
            transaction['signature'] = signature
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
            db_handler.execute_param(db_handler.h3,
                                    "SELECT timestamp, recipient FROM transactions WHERE block_height= ? AND reward > 0",
                                    (raw[0],))
            block_data = db_handler.h3.fetchone()
            transaction['blocktime'] = block_data[0]
            transaction['blockminer'] = block_data[1]
            print('api_gettransactionbysignature', format, transaction)
            connections.send(socket_handler, transaction)
        except Exception as e:
            raise

    def api_getpeerinfo(self, socket_handler, db_handler, peers):
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

def api_gettransaction_for_recipients(self, socket_handler, db_handler, peers):
        """
        Warning: this is currently very slow
        Returns the full transaction matching a tx id for a list of recipient addresses. 
        Takes txid anf format as params (json output if format is True)        
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
            recipients = json.dumps(addresses).replace("[", "(").replace(']', ')')  # format as sql
            # raw tx details
            db_handler.execute_param(db_handler.h3,
                                    "SELECT * FROM transactions WHERE recipient IN {} AND signature LIKE ?".format(recipients),
                                    (transaction_id + '%', ))
            raw = db_handler.h3.fetchone()
            if not format:
                connections.send(socket_handler, raw)
                print('api_gettransaction_for_recipients', format, raw)
                return

            # current block height, needed for confirmations #
            db_handler.execute(db_handler.h3, "SELECT MAX(block_height) FROM transactions")
            block_height = db_handler.h3.fetchone()[0]
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
            db_handler.execute_param(db_handler.h3,
                                    "SELECT timestamp, recipient FROM transactions WHERE block_height= ? AND reward > 0",
                                    (raw[0],))
            block_data = db_handler.h3.fetchone()
            transaction['blocktime'] = block_data[0]
            transaction['blockminer'] = block_data[1]
            print('api_gettransaction_for_recipients', format, transaction)
            connections.send(socket_handler, transaction)
        except Exception as e:
            raise
