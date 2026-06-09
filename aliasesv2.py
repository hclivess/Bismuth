"""Alias index builder (``alias:register`` operation convention).

Scans the ledger for transactions whose ``operation`` is ``alias:register``
and records each first-seen ``alias -> address`` mapping in the ``aliases``
index table. Registrations are processed in block/timestamp order and the
first claimant of a given alias wins (later duplicates are ignored).

This builds a derived index only and does not affect consensus. It handles the
newer operation-based format; see :mod:`aliases` for the older ``alias=<name>``
openfield-based format.
"""

import log


def aliases_update(node, db_handler_instance):
            
    db_handler_instance.index_cursor.execute("SELECT block_height FROM aliases ORDER BY block_height DESC LIMIT 1;")
    try:
        alias_last_block = int(db_handler_instance.index_cursor.fetchone()[0])
    except:
        alias_last_block = 0

    node.logger.app_log.warning("Alias anchor block: {}".format(alias_last_block))
    
    db_handler_instance.h.execute("SELECT block_height, address, openfield FROM transactions WHERE operation = ? AND block_height >= ? AND reward = 0 ORDER BY block_height ASC, timestamp ASC;", ("alias:register", alias_last_block,))
    #include the anchor block in case indexation stopped there
    result = db_handler_instance.h.fetchall()
    
    for openfield in result:
        node.logger.app_log.warning(f"Processing alias registration: {openfield[2]}")
        try:
            db_handler_instance.index_cursor.execute("SELECT * from aliases WHERE alias = ?", (openfield[2],))
            dummy = db_handler_instance.index_cursor.fetchall()[0] #check for uniqueness
            node.logger.app_log.warning(f"Alias already registered: {openfield[2]}")
        except:
            db_handler_instance.index_cursor.execute("INSERT INTO aliases VALUES (?,?,?)", (openfield[0],openfield[1],openfield[2]))
            db_handler_instance.index.commit()
            node.logger.app_log.warning(f"Added alias to the database: {openfield[2]} from block {openfield[0]}")


if __name__ == "__main__":
    app_log = log.log("aliases.log", "WARNING", True)
