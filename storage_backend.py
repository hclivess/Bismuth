"""Storage read seam (doc/26 stage 3) — one block/tx read interface, two backends.

The endgame of doc/26 is a node whose canonical store is LMDB, not the SQLite trio. Getting there safely
means a **strangler-fig seam**: route reads through a thin interface with two implementations that return
BYTE-IDENTICAL results, run them side by side with a cross-check, migrate call sites one at a time, and only
then delete the SQLite path. This module is that interface plus the two backends, for the **block/tx read
surface** (the part the LMDB ``block_store`` already serves 1:1):

  * ``SqliteBackend``  — a read-only cursor over the legacy ``ledger.db`` ``transactions`` table; EXACT current
    behavior, the pre-fork / compatibility path.
  * ``LmdbBackend``    — the ``block_store.BlockStore`` (LMDB), the post-fork canonical path.

Both return the same 12-field ledger rows (``block_store`` re-prepends block_height and re-expands the
pubkey, so the rows match SQLite exactly — proven by ``block_store.verify_against_sqlite`` and ``cross_check``
here). This is additive and consensus-inert: it is NOT yet wired into the hot path; reads migrate onto it
incrementally (REST/display first, then sync, then consensus), each cross-checked, per doc/26 §3.

Balance reads are deliberately OUT of scope here — they have their own maintained index
(``balance_index``, LMDB) and a separate (more delicate) consensus computation (``ledger_balance3``); they
get their own seam increment.
"""

__version__ = "0.1.0"


class StorageBackend:
    """Read interface over canonical block/tx data. Implementations MUST return identical results."""

    def get_block(self, height):
        """The full 12-field ledger rows at ``height`` (a list of lists), or None if absent."""
        raise NotImplementedError

    def block_hash(self, height):
        """The block hash at ``height`` (str), or None."""
        raise NotImplementedError

    def height_by_hash(self, block_hash):
        """The height of the block with ``block_hash`` (int), or None."""
        raise NotImplementedError

    def tip_height(self):
        """The highest stored block height (int), or None on an empty store."""
        raise NotImplementedError

    def blocks_in_range(self, start, end):
        """Yield ``(height, rows)`` for heights in [start, end], ascending."""
        raise NotImplementedError

    def close(self):
        pass


class SqliteBackend(StorageBackend):
    """Legacy ledger.db reader — a fresh read-only connection (never holds the node's write lock). Returns
    rows EXACTLY as the SQLite ledger holds them (``SELECT *`` column order, rowid order within a block), so
    it is the behavioral reference the LMDB backend is verified against."""

    def __init__(self, ledger_path):
        import sqlite3
        self.ledger_path = ledger_path
        self.conn = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=60)
        self.conn.text_factory = str

    def get_block(self, height):
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE block_height = ? ORDER BY rowid", (int(height),)).fetchall()
        return [list(r) for r in rows] if rows else None

    def block_hash(self, height):
        r = self.conn.execute(
            "SELECT block_hash FROM transactions WHERE block_height = ? ORDER BY rowid LIMIT 1",
            (int(height),)).fetchone()
        return r[0] if r else None

    def height_by_hash(self, block_hash):
        r = self.conn.execute(
            "SELECT block_height FROM transactions WHERE block_hash = ? LIMIT 1", (block_hash,)).fetchone()
        return r[0] if r else None

    def tip_height(self):
        r = self.conn.execute("SELECT max(block_height) FROM transactions").fetchone()
        return r[0] if r and r[0] is not None else None

    def blocks_in_range(self, start, end):
        rows = self.conn.execute(
            "SELECT * FROM transactions WHERE block_height >= ? AND block_height <= ? ORDER BY block_height, rowid",
            (int(start), int(end))).fetchall()
        cur_h, group = None, None
        for r in rows:
            if r[0] != cur_h:
                if group:
                    yield cur_h, group
                cur_h, group = r[0], []
            group.append(list(r))
        if group:
            yield cur_h, group

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class LmdbBackend(StorageBackend):
    """Post-fork canonical reader — delegates straight to the LMDB ``block_store`` (which already returns the
    same 12-field rows)."""

    def __init__(self, block_store):
        self.store = block_store

    def get_block(self, height):
        return self.store.get_block(int(height))

    def block_hash(self, height):
        return self.store.block_hash(int(height))

    def height_by_hash(self, block_hash):
        return self.store.height_by_hash(block_hash)

    def tip_height(self):
        return self.store.tip()

    def blocks_in_range(self, start, end):
        return self.store.blocks_in_range(int(start), int(end))

    # close() intentionally omitted: the node owns the block_store lifecycle, not this wrapper.


def cross_check(reference: StorageBackend, candidate: StorageBackend, start, end):
    """Assert ``candidate`` returns block/tx data identical to ``reference`` for heights [start, end].
    Returns (blocks_checked, txs_checked); raises AssertionError on the first mismatch. This is how a read is
    proven safe to migrate (and how the two run side by side during the transition)."""
    blocks = txs = 0
    for height, rows in reference.blocks_in_range(start, end):
        got = candidate.get_block(height)
        assert got is not None, "block %d missing from candidate backend" % height
        assert got == rows, "block %d differs between backends" % height
        assert candidate.block_hash(height) == rows[0][7], "block %d hash differs" % height
        blocks += 1
        txs += len(rows)
    return blocks, txs


def select(node) -> StorageBackend:
    """Pick the backend for ``node``: the LMDB block store post-fork when it is present (the canonical
    post-fork store), else the legacy SQLite ledger. Mirrors how the other LMDB stores gate on the fork
    height; pre-fork and on a node without a block store this is the SqliteBackend, i.e. today's behavior.

    NOTE: this selector is the wiring point for the incremental read migration (doc/26 stage 3). It is not
    yet called from the hot path — reads are moved onto it one surface at a time, cross-checked first."""
    fork_height = getattr(node, "fork_height", None)
    last_block = getattr(node, "last_block", 0) or 0
    store = getattr(node, "block_store", None)
    post_fork = fork_height is not None and last_block >= fork_height
    if post_fork and store is not None:
        return LmdbBackend(store)
    return SqliteBackend(node.ledger_path)


# ===================================================================================================
# Write seam (doc/26 stage 4). Today the canonical block WRITE is the legacy two-phase SQLite path
# (`DbHandler.to_db` -> working store, then `db_to_drive` -> ledger.db) kept consistent across the two
# files by the `commit_marker`/`ATTACH` lockstep — the machinery whose split caused the 19h outage. The
# end state is ONE LMDB store written in ONE atomic txn (no second file, no lockstep). This interface makes
# the block-body write a pluggable primitive; `LmdbWriteBackend` is that single-atomic-txn write.
#
# It does NOT yet REPLACE the SQLite write: the consensus READS still hit SQLite — above all the overspend
# check (`ledger_balance3`), which is deliberately kept authoritative (a wrong balance index would be
# inflation). So SQLite stays the source of truth until those reads migrate (the rest of stage 3) AND the
# LMDB block write is continuously cross-checked (`cross_check`) long enough to be trusted as canonical.
# This seam is the foundation for that flip; for now the digester drives it as the (reorg-safe) block-body
# store, alongside the unchanged SQLite path.
# ===================================================================================================


class StorageWriteBackend:
    """Write interface for canonical block bodies."""

    def append_block(self, height, block_hash, rows):
        """Persist a block's full 12-field rows at ``height`` (with ``block_hash``)."""
        raise NotImplementedError

    def rollback(self, to_height):
        """Drop every block above ``to_height`` (a reorg). Returns the count removed."""
        raise NotImplementedError

    def committed_tip(self):
        """The highest committed block height (int), or None — the post-fork recovery anchor candidate."""
        raise NotImplementedError


class LmdbWriteBackend(StorageWriteBackend):
    """The post-fork canonical block-body write: ONE LMDB store, ONE atomic txn per block — no
    `commit_marker`, no `ATTACH`, no two-file gap. Append and height-range rollback are exactly
    `block_store`'s atomic ops (a crash leaves the store at a clean block boundary, so the tip is an
    unambiguous recovery floor — the property that retires the SQLite lockstep once SQLite is gone)."""

    def __init__(self, block_store, node=None):
        self.store = block_store
        # node is read (not captured) so fork_height is the LIVE value at append time — the destination-
        # height gate for hf2 Stage-4 true-bytes storage (doc/40). node=None => fork_height None => legacy.
        self._node = node

    def append_block(self, height, block_hash, rows):
        fork_height = getattr(self._node, "fork_height", None) if self._node is not None else None
        self.store.put_block(height, block_hash, rows, fork_height=fork_height)

    def rollback(self, to_height):
        return self.store.rollback(to_height)

    def committed_tip(self):
        return self.store.tip()
