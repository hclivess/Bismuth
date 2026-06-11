"""Legacy alias index builder (``alias=`` openfield convention).

Scans the ledger for transactions whose ``openfield`` starts with ``alias=``
and records each first-seen ``alias -> address`` mapping in the ``aliases``
index table. Registrations are processed in block/timestamp order and the
first claimant of a given alias wins (later duplicates are ignored).

This builds a derived index only and does not affect consensus. It handles the
older ``alias=<name>`` openfield format; see :mod:`aliasesv2` for the newer
``alias:register`` operation-based format.

STORAGE SEAM (doc/26 stage 2): when ``node.token_index`` is set, registrations go into the isolated
LMDB side-index (``token_index.TokenIndex``) instead of the SQLite ``index.db`` — same scan, same
first-claimant-wins rule, NO SQLite. When it is ``None`` the legacy ``index.db`` path runs unchanged.
"""

import log

from essentials import replace_regex  # shared, lru-cached implementation (was duplicated here)


def aliases_update(node, db_handler_instance):
    """Bring the alias index up to date. When the **tokens_aliases plugin** owns the index (``node.token_index``
    set, doc/27) the core does NOTHING here — the plugin maintains its isolated LMDB store per-block via
    ``on_block`` (and backfilled history at startup). Otherwise the legacy SQLite ``index.db`` path runs,
    byte-for-byte the original (pre-fork compatibility for old peers)."""
    if getattr(node, "token_index", None) is not None:
        return
    return _aliases_update_sqlite(node, db_handler_instance)


def _aliases_update_sqlite(node, db_handler_instance):
    db_handler_instance.index_cursor.execute("SELECT block_height FROM aliases ORDER BY block_height DESC LIMIT 1;")
    try:
        alias_last_block = int(db_handler_instance.index_cursor.fetchone()[0])
    except:
        alias_last_block = 0

    node.logger.app_log.warning("Alias anchor block: {}".format(alias_last_block))

    db_handler_instance.h.execute("SELECT block_height, address, openfield FROM transactions WHERE openfield LIKE ? AND block_height >= ? ORDER BY block_height ASC, timestamp ASC;", ("alias=" + '%',) + (alias_last_block,))
    # include the anchor block in case indexation stopped there
    result = db_handler_instance.h.fetchall()

    for openfield in result:
        alias = (replace_regex(openfield[2], "alias="))
        node.logger.app_log.warning(f"Processing alias registration: {alias}")
        try:
            db_handler_instance.index_cursor.execute("SELECT * from aliases WHERE alias = ?", (alias,))
            dummy = db_handler_instance.index_cursor.fetchall()[0]  # check for uniqueness
            node.logger.app_log.warning(f"Alias already registered: {alias}")
        except:
            db_handler_instance.index_cursor.execute("INSERT INTO aliases VALUES (?,?,?)", (openfield[0], openfield[1], alias))
            db_handler_instance.index.commit()
            node.logger.app_log.warning(f"Added alias to the database: {alias} from block {openfield[0]}")


if __name__ == "__main__":
    app_log = log.log("aliases.log", "WARNING", True)

