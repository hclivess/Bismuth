"""
Block-oriented read API (``api_getblock*``) methods for ``ApiHandler``, split out of the apihandler god-class as a mixin and
recombined via ``class ApiHandler(BlockApiMixin, ...)``. ``__slots__ = ()`` preserves the parent's slot
layout (no per-instance ``__dict__``); the methods are unchanged and run on the composed ApiHandler
instance, so ``self.app_log``/``self.config`` and cross-domain ``self.api_*`` calls resolve via the MRO.
"""
import json
import os
import re
import sys
import amounts
import block_format
import connections


def _store(db_handler, node=None):
    """The LMDB ledger (``block_store.BlockStore``) behind a DbHandler (or the node's, as a fallback)."""
    store = getattr(db_handler, "store", None)
    if store is None and node is not None:
        store = getattr(node, "ledger", None)
    return store


def _like(pattern):
    """A matcher with the legacy ``LIKE`` semantics the API exposed: ``%`` / ``_`` wildcards, ASCII
    case-insensitive, whole-string."""
    rx = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in str(pattern))
    return re.compile(rx, re.ASCII | re.IGNORECASE | re.DOTALL).fullmatch


def _rows_in_range(store, lo, hi):
    """Every typed row in blocks [lo, hi], ascending."""
    out = []
    if hi < lo:
        return out
    for _h, rows in store.blocks_in_range(lo, hi):
        out.extend(rows)
    return out


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

        store = _store(db_handler, getattr(self, "node", None))
        result = []
        try:
            h = store.height_by_hash(block_hash)
        except Exception:
            h = None
        if h is not None:
            result = store.get_block(h) or []
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

            store = _store(db_handler, getattr(self, "node", None))
            try:
                h = store.height_by_hash(block_hash)
            except Exception:
                h = None
            result = (store.get_block(h) or []) if h is not None else []
            blocks = block_format.blockstojson(result)
            if not blocks:
                # Unknown or pruned hash: reply explicitly so the client is not left blocking.
                connections.send(socket_handler, None)
                return
            block = list(blocks.values())[0]

            height = int(block['block_height'])
            block["previous_block_hash"] = store.block_hash(height - 1) if height > 0 else None
            block["next_block_hash"] = store.block_hash(height + 1)
            difficulty_row = store.difficulty(height)
            block["difficulty"] = int(float(difficulty_row)) if difficulty_row is not None else 0
            # print(block)
            connections.send(socket_handler, block)
        except Exception as e:
            self.app_log.warning("api_getblockfromhashextra {}".format(e))
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            self.app_log.warning("{} {} {}".format(exc_type, fname, exc_tb.tb_lineno))
            # Always reply so a request/response client is not left blocking until timeout.
            try:
                connections.send(socket_handler, None)
            except Exception:
                pass

    def api_getblockfromheight(self, socket_handler, db_handler, peers):
        """
        Returns a specific block based on the provided hash.

        :param socket_handler:
        :param db_handler:
        :param peers:
        :return:
        """

        height = connections.receive(socket_handler)

        store = _store(db_handler, getattr(self, "node", None))
        try:
            result = store.get_block(int(height)) or []
        except (ValueError, TypeError):
            result = []
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
            store = _store(db_handler, getattr(self, "node", None))
            start_block, limit = int(start_block), int(limit)
            diffs = dict(store.difficulty_range(start_block, start_block + limit))
            raw_txs, raw_diffs = [], []
            for h, rows in store.blocks_in_range(start_block, start_block + limit - 1):
                raw_txs.extend(rows)
                # one mining tx per block: keep the difficulty list aligned with the mining txs
                raw_diffs.extend((diffs.get(h),) for r in rows if r[9] != 0)

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
                store = _store(db_handler, getattr(self, "node", None))
                tip = store.tip()
                # what is the min block height to consider ?
                block_height = max(tip - 11, since_height)
                info = [amounts.display_row(r) for r in _rows_in_range(store, int(block_height) + 1, tip)]
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
        where_openfield_like = _like(connections.receive(socket_handler) + '%')
        #print('api_getblockswhereoflike', since_height, where_openfield_like)
        try:
            try:
                store = _store(db_handler, getattr(self, "node", None))
                # what is the max block height to consider ?
                block_height = min(store.tip(), since_height+1440)
                info = [amounts.display_row(r) for r in _rows_in_range(store, since_height + 1, block_height)
                        if where_openfield_like(str(r[11]))]
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
        self.app_log.warning(f"api_getblocksafterwhere {since_height} {where_conditions}")
        # Free-form where-conditions were never safe to evaluate; the command stays disabled.
        raise ValueError("Unsafe, do not use yet")
