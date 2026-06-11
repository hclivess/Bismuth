"""
Block-oriented read API (``api_getblock*``) methods for ``ApiHandler``, split out of the apihandler god-class as a mixin and
recombined via ``class ApiHandler(BlockApiMixin, ...)``. ``__slots__ = ()`` preserves the parent's slot
layout (no per-instance ``__dict__``); the methods are unchanged and run on the composed ApiHandler
instance, so ``self.app_log``/``self.config`` and cross-domain ``self.api_*`` calls resolve via the MRO.
"""
import json
import os
import sys
import amounts
import block_format
import connections


def _seam_backend(node):
    """Storage read seam (doc/26 stage 3): the LMDB block store post-fork when present, else None (the caller
    falls back to the legacy SQLite ledger cursor — unchanged pre-fork). The socket analog of rest_api's
    _store_backend; this is the legacy wire protocol, migrated for parity."""
    if node is None:
        return None
    store = getattr(node, "block_store", None)
    fork_height = getattr(node, "fork_height", None)
    last_block = getattr(node, "last_block", 0) or 0
    if store is not None and fork_height is not None and last_block >= fork_height:
        import storage_backend
        return storage_backend.LmdbBackend(store)
    return None


class BlockApiMixin:
    __slots__ = ()

    def api_getblockfromhash(self, socket_handler, db_handler, peers):
        """
        Returns a specific block based on the provided hash.
        Warning: format is strange: we provide a hash, so there should be at most one result.
        Or we send back a dict, with height as key, and block (including height again) as value.
        Should be enough to only send the block.
        **BUT** do not change, this would break current implementations using the current format (json rpc server for instance).

        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """

        block_hash = connections.receive(socket_handler)

        # seam (doc/26): LMDB block store post-fork, SQLite fallback — same rows, same blockstojson output
        result = None
        be = _seam_backend(getattr(self, "node", None))
        if be is not None:
            h = be.height_by_hash(block_hash)
            if h is not None:
                result = be.get_block(h)
        if not result:
            db_handler.execute_param(db_handler.h,
                                     "SELECT * FROM transactions "
                                     "WHERE block_hash = ?",
                                     (block_hash,))
            result = db_handler.h.fetchall()
        blocks = block_format.blockstojson(result)
        connections.send(socket_handler, blocks)

    def api_getblockfromhashextra(self, socket_handler, db_handler, peers):
        """
        Returns a specific block based on the provided hash.
        similar to api_getblockfromhash, but sends block dict, not a dict of a dict.
        Also embeds last and next block hash, as well as block difficulty
        Needed for json-rpc server and btc like data.

        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        try:
            block_hash = connections.receive(socket_handler)

            result = db_handler.fetchall(db_handler.h,
                                         "SELECT * FROM transactions "
                                         "WHERE block_hash = ? ",
                                         (block_hash,))
            blocks = block_format.blockstojson(result)
            block = list(blocks.values())[0]

            block["previous_block_hash"] = db_handler.fetchone(db_handler.h,
                                                               "SELECT block_hash FROM transactions WHERE block_height = ?",
                                                               (block['block_height'] - 1,))
            block["next_block_hash"] = db_handler.fetchone(db_handler.h,
                                                           "SELECT block_hash FROM transactions WHERE block_height = ?",
                                                           (block['block_height'] + 1,))
            block["difficulty"] = int(float(db_handler.fetchone(db_handler.h,
                                                                "SELECT difficulty FROM misc WHERE block_height = ?",
                                                                (block['block_height'],))))
            # print(block)
            connections.send(socket_handler, block)
        except Exception as e:
            self.app_log.warning("api_getblockfromhashextra {}".format(e))
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            self.app_log.warning("{} {} {}".format(exc_type, fname, exc_tb.tb_lineno))
            raise

    def api_getblockfromheight(self, socket_handler, db_handler, peers):
        """
        Returns a specific block based on the provided hash.

        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """

        height = connections.receive(socket_handler)

        # seam (doc/26): LMDB block store post-fork, SQLite fallback — same rows, same blockstojson output
        result = None
        be = _seam_backend(getattr(self, "node", None))
        if be is not None:
            try:
                result = be.get_block(int(height))
            except (ValueError, TypeError):
                result = None
        if not result:
            db_handler.execute_param(db_handler.h, ("SELECT * FROM transactions "
                                                    "WHERE block_height = ? "),
                                     (height,))
            result = db_handler.h.fetchall()
        blocks = block_format.blockstojson(result)
        connections.send(socket_handler, blocks)

    def api_getblockrange(self, socket_handler, db_handler, peers):
        """
        Returns full blocks and transactions from a block range, maximum of 50 entries.
        Includes function format_raw_txs_diffs for formatting. Useful for big data / nosql storage.
        :param socket_handler:
        :param db_handler: (UNUSED)
        :param peers: (UNUSED)
        :return:
        """

        start_block = connections.receive(socket_handler)
        limit = connections.receive(socket_handler)

        if limit > 50:
            limit = 50

        try:
            db_handler.execute_param(db_handler.h,
                                     ('SELECT * FROM transactions '
                                      'WHERE block_height >= ? '
                                      'AND block_height < ?;'),
                                     (start_block, start_block+limit,))
            raw_txs = db_handler.h.fetchall()

            db_handler.execute_param(db_handler.h,
                                     ('SELECT difficulty FROM misc '
                                      'WHERE block_height >= ? '
                                      'AND block_height < ?;'),
                                     (start_block, start_block+limit,))
            raw_diffs = db_handler.h.fetchall()

            reply = json.dumps(block_format.blocktojsondiffs(raw_txs, raw_diffs))

        except Exception as e:
            self.app_log.warning(e)
            raise
        connections.send(socket_handler, reply)

    def api_getblocksince(self, socket_handler, db_handler, peers):
        """
        Returns the full blocks and transactions following a given block_height
        Returns at most transactions from 10 blocks (the most recent ones if it truncates)
        Used by the json-rpc server to poll and be notified of tx and new blocks.

        Returns full blocks and transactions following a given block_height.
        Given block_height should not be lower than the last 10 blocks.
        If given block_height is lower than the most recent block -10,
        last 10 blocks will be returned.

        **Used by the json-rpc server to poll and be notified of tx and new blocks** DO NOT REMOVE!!!.
        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """
        info = []
        # get the last known block
        since_height = connections.receive(socket_handler)
        # print('api_getblocksince', since_height)
        try:
            try:
                db_handler.execute(db_handler.h, "SELECT MAX(block_height) FROM transactions")
                # what is the min block height to consider ?
                block_height = max(db_handler.h.fetchone()[0]-11, since_height)
                db_handler.execute_param(db_handler.h,
                                        ('SELECT * FROM transactions WHERE block_height > ?;'),
                                        (block_height, ))
                info = [amounts.display_row(r) for r in db_handler.h.fetchall()]
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
                db_handler.execute(db_handler.h, "SELECT MAX(block_height) FROM transactions")
                # what is the max block height to consider ?
                block_height = min(db_handler.h.fetchone()[0], since_height+1440)
                #print("block_height", since_height, block_height)
                db_handler.execute_param(db_handler.h,
                                        'SELECT * FROM transactions WHERE block_height > ? and block_height <= ? and openfield like ?',
                                        (since_height, block_height, where_openfield_like) )
                info = [amounts.display_row(r) for r in db_handler.h.fetchall()]
                # it's a list of tuples, send as is.
                #print("info", info)
            except Exception as e:
                self.app_log.warning(e)
                raise
            # Add the last fetched block so the client will be able to fetch the next block
            info.append([block_height])
            connections.send(socket_handler, info)
        except Exception as e:
            self.app_log.warning(e)
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            self.app_log.warning("{} {} {}".format(exc_type, fname, exc_tb.tb_lineno))
            raise

    def api_getblocksafterwhere(self, socket_handler, db_handler, peers):
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
        self.app_log.warning('api_getblocksafterwhere', since_height, where_conditions)
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
                db_handler.execute(db_handler.h, "SELECT MAX(block_height) FROM transactions")
                # what is the max block height to consider ?
                block_height = min(db_handler.h.fetchone()[0], since_height+720)
                # print("block_height",block_height)
                db_handler.execute_param(db_handler.h,
                                        ('SELECT * FROM transactions WHERE block_height > ? and block_height <= ? and ( '+where_assembled+')'),
                                        (since_height, block_height)+conditions_assembled)
                info = [amounts.display_row(r) for r in db_handler.h.fetchall()]
                # it's a list of tuples, send as is.
                # print(all)
            except Exception as e:
                self.app_log.warning(e)
                raise
            # print("info", info)
            connections.send(socket_handler, info)
        except Exception as e:
            # self.app_log.warning(e)
            raise
