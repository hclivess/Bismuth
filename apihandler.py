"""
API Command handler module for Bismuth nodes
@EggPoolNet
Needed for Json-RPC server or other third party interaction

The ``api_*`` command surface is split across domain mixins (blocks / address / transactions) that are
recombined here; this module keeps the dispatcher and the small control commands. ``dispatch`` resolves
``getattr(self, method)`` over the full composed class, so every mixin method is reachable by name.
"""
import threading

import connections
import mempool as mp
from apihandler_blocks import BlockApiMixin
from apihandler_address import AddressApiMixin
from apihandler_tx import TxApiMixin

__version__ = "0.0.9"


class ApiHandler(BlockApiMixin, AddressApiMixin, TxApiMixin):
    """
    The API commands manager. Extra commands, not needed for node communication, but for third party tools.
    Handles all commands prefixed by "api_".
    It's called from client threads, so it has to be thread safe.
    """

    __slots__ = ('app_log', 'config', 'node', 'callback_lock', 'callbacks')

    def __init__(self, app_log, config=None, node=None):
        self.app_log = app_log
        self.config = config
        self.node = node          # for the storage read seam (doc/26): LMDB block reads post-fork
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
            # raise
            self.app_log.warning(f"API Method <{method}> does not exist.")
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

    def api_getconfig(self, socket_handler, db_handler, peers):
        """
        Returns configuration
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return: list of node configuration options
        """
        connections.send(socket_handler, self.config.__dict__)

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
            self.app_log.warning(e)
