"""
Ledger access for the node: ``DbHandler`` is a thin, thread-safe facade over the ONE LMDB ledger
(``block_store.BlockStore``). It keeps the semantic read helpers the node has always called
(``last_block_hash``, ``pubkeyget``, ``blocksync`` …), the block commit, and the reorg rollback. Every
handler shares the process-wide store (``block_store.open_ledger``), so constructing one is free.
"""
import sys

import essentials
from quantizer import quantize_two

import block_store


class DbHandler:
    def __init__(self, store, logger=None, node=None):
        self.store = store
        self.logger = logger
        self.node = node
        self._pubkey_cache = {}

    @classmethod
    def for_node(cls, node):
        return cls(node.ledger, getattr(node, "logger", None), node=node)

    def _fork_height(self):
        return getattr(self.node, "fork_height", None) if self.node is not None else None

    # ------------------------------------------------------------------ tip
    def block_height_max(self):
        return self.store.tip()

    def block_height_max_diff(self):
        return self.store.last_difficulty()[0]

    def last_block_hash(self):
        tip = self.store.tip()
        return None if tip is None else self.store.block_hash(tip)

    def last_block_timestamp(self):
        tip = self.store.tip()
        cb = self.store.coinbase(tip) if tip is not None else None
        return quantize_two(cb[block_store.TS]) if cb else quantize_two(0)

    def last_mining_tx(self):
        """The tip block's mining tx (typed row)."""
        tip = self.store.tip()
        return self.store.coinbase(tip) if tip is not None else None

    def block_max_ram(self):
        """The tip block's last row, formatted (block_height / block_hash / …)."""
        tip = self.store.tip()
        rows = self.store.get_block(tip) if tip is not None else None
        return essentials.format_raw_tx(rows[-1]) if rows else None

    def difflast(self):
        return self.store.last_difficulty()

    # ------------------------------------------------------------------ lookups
    def block_height_from_hash(self, block_hash):
        try:
            return self.store.height_by_hash(block_hash)
        except Exception:
            return None

    def pubkeyget(self, address):
        if address in self._pubkey_cache:
            return self._pubkey_cache[address]
        result = self.store.first_pubkey(address)
        if result is None:
            raise ValueError(f"no public key on the ledger for {address}")
        self._pubkey_cache[address] = result
        return result

    def _genesis_op(self, node, operation, default):
        for r in self.store.address_rows(node.genesis, role="sender", limit=None):
            if r[block_store.OP] == operation:
                return r[block_store.OF]
        return default

    def annverget(self, node):
        return self._genesis_op(node, "annver", "?")

    def annget(self, node):
        return self._genesis_op(node, "ann", "No announcement")

    def blocksync(self, block):
        """The blocks after ``block`` in the 8-field sync form, capped at ~500 KB of payload."""
        blocks_fetched = []
        total_size = 0
        h = int(block) + 1
        while total_size < 500000:
            rows = self.store.get_block(h)
            if not rows:
                break
            result = [(r[1], r[2], r[3], r[4], r[5], r[6], r[10], r[11]) for r in rows]
            blocks_fetched.append(result)
            total_size += sys.getsizeof(str(result))
            h += 1
        return blocks_fetched

    def latest_rows(self, limit):
        """The newest ``limit`` ledger rows (highest blocks first, each block's rows in order)."""
        out = []
        limit = int(limit)
        for _h, rows in self.store.blocks_desc():
            out.extend(rows)
            if len(out) >= limit:
                break
        return out[:limit]

    def address_rows(self, address, limit=None):
        """Rows where ``address`` is sender or recipient, newest first."""
        return self.store.address_rows(address, limit=int(limit) if limit else None)

    def balance_get(self, address):
        return self.store.balance(address)

    # ------------------------------------------------------------------ tokens / aliases (plugin store)
    def _ti(self):
        return getattr(self.node, "token_index", None) if self.node is not None else None

    def addfromalias(self, alias):
        ti = self._ti()
        return ti.addfromalias(alias) if ti is not None else "No alias"

    def aliasget(self, alias_address):
        ti = self._ti()
        return ti.aliasget(alias_address) if ti is not None else [[alias_address]]

    def aliasesget(self, aliases_request):
        ti = self._ti()
        return ti.aliasesget(aliases_request) if ti is not None else list(aliases_request)

    def tokens_user(self, tokens_address):
        ti = self._ti()
        return ti.tokens_user(tokens_address) if ti is not None else []

    def tokens_rollback(self, node, height):
        ti = getattr(node, "token_index", None)
        if ti is None:
            return
        try:
            ti.tokens_rollback(height)
            node.logger.app_log.warning(f"Rolled back the token index below {height}")
        except Exception as e:
            node.logger.app_log.warning(f"Failed to roll back the token index below {height}: {e}")

    def aliases_rollback(self, node, height):
        ti = getattr(node, "token_index", None)
        if ti is None:
            return
        try:
            ti.aliases_rollback(height)
            node.logger.app_log.warning(f"Rolled back the alias index below {height}")
        except Exception as e:
            node.logger.app_log.warning(f"Failed to roll back the alias index below {height}: {e}")

    # ------------------------------------------------------------------ write
    def commit_block(self, height, block_hash, rows, difficulty):
        """Commit one validated block (rows + difficulty + every index) in ONE ledger transaction."""
        self.store.commit_block(height, block_hash, rows, difficulty=difficulty,
                                fork_height=self._fork_height())
        self.clear_caches()

    def rollback_under(self, block_height):
        """Drop every block at or above ``block_height``; returns the removed rows (ascending)."""
        removed = self.store.rollback(int(block_height) - 1, fork_height=self._fork_height())
        self.clear_caches()
        return removed

    def backup_higher(self, block_height):
        return self.rollback_under(block_height)

    def clear_caches(self):
        self._pubkey_cache.clear()

    def close(self):
        # the store is process-wide; handlers own nothing
        pass
