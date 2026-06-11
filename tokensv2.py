"""Token (v2) index builder for the Bismuth ledger.

Scans the ``transactions`` table for ``token:issue`` and ``token:transfer``
operations (encoded in a transaction's ``operation``/``openfield`` columns)
and maintains a derived ``tokens`` table in the index database. Token
issuances register a new token name with its total supply; transfers debit
the sender and credit the recipient, with on-the-fly balance checks so that
overspends are recorded as no-ops (preventing re-processing) rather than
applied.

This is a derived index only: it never touches the canonical ledger and has
no effect on consensus. It is rebuilt incrementally from the last anchored
``block_height`` on every call.

Operation / openfield encoding:
    token:issue     -> openfield = ``token_name:total_number``
    token:transfer  -> openfield = ``token_name:amount``

STORAGE SEAM (doc/26 stage 2): when ``node.token_index`` is set, the projection is built into the
isolated LMDB side-index (``token_index.TokenIndex``) instead of the SQLite ``index.db`` — same scan,
same overspend rule, NO SQLite. When it is ``None`` the legacy ``index.db`` path runs unchanged.
"""

import log
from hashlib import blake2b

__version__ = '0.0.3'


def blake2bhash_generate(data):
    # new hash
    blake2bhash = blake2b(str(data).encode(), digest_size=20).hexdigest()
    return blake2bhash
    # new hash


def tokens_update(node, db_handler_instance):
    """Bring the token index up to date. When the **tokens_aliases plugin** owns the index (``node.token_index``
    set, doc/27) the core does NOTHING here — the plugin maintains its isolated LMDB store per-block via
    ``on_block`` (and backfilled history at startup). Otherwise the legacy SQLite ``index.db`` path runs,
    byte-for-byte the original (pre-fork compatibility for old peers)."""
    if getattr(node, "token_index", None) is not None:
        return
    return _tokens_update_sqlite(node, db_handler_instance)


def _tokens_update_sqlite(node, db_handler_instance):

    db_handler_instance.index_cursor.execute("CREATE TABLE IF NOT EXISTS tokens (block_height INTEGER, timestamp, token, address, recipient, txid, amount INTEGER)")
    db_handler_instance.index.commit()

    db_handler_instance.index_cursor.execute("SELECT block_height FROM tokens ORDER BY block_height DESC LIMIT 1;")
    try:
        token_last_block = int(db_handler_instance.index_cursor.fetchone()[0])
    except:
        token_last_block = 0

    node.logger.app_log.warning("Token anchor block: {}".format(token_last_block))

    # node.logger.app_log.warning all token issuances
    db_handler_instance.c.execute("SELECT block_height, timestamp, address, recipient, signature, operation, openfield FROM transactions WHERE block_height >= ? AND operation = ? AND reward = 0 ORDER BY block_height ASC;", (token_last_block, "token:issue",))
    results = db_handler_instance.c.fetchall()
    node.logger.app_log.warning(results)

    tokens_processed = []

    for x in results:
        try:
            token_name = x[6].split(":")[0].lower().strip()
            try:
                db_handler_instance.index_cursor.execute("SELECT * from tokens WHERE token = ?", (token_name,))
                dummy = db_handler_instance.index_cursor.fetchall()[0]  # check for uniqueness
                node.logger.app_log.warning("Token issuance already processed: {}".format(token_name,))
            except:
                if token_name not in tokens_processed:
                    block_height = x[0]
                    node.logger.app_log.warning("Block height {}".format(block_height))

                    timestamp = x[1]
                    node.logger.app_log.warning("Timestamp {}".format(timestamp))

                    tokens_processed.append(token_name)
                    node.logger.app_log.warning("Token: {}".format(token_name))

                    issued_by = x[3]
                    node.logger.app_log.warning("Issued by: {}".format(issued_by))

                    txid = x[4][:56]
                    node.logger.app_log.warning("Txid: {}".format(txid))

                    total = x[6].split(":")[1]
                    node.logger.app_log.warning("Total amount: {}".format(total))

                    db_handler_instance.index_cursor.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?)",
                              (block_height, timestamp, token_name, "issued", issued_by, txid, total))

                    if node.plugin_manager:
                        node.plugin_manager.execute_action_hook('token_issue',
                                                           {'token': token_name, 'issuer': issued_by,
                                                            'txid': txid, 'total': total})

                else:
                    node.logger.app_log.warning("This token is already registered: {}".format(x[1]))
        except:
            node.logger.app_log.warning("Error parsing")

    db_handler_instance.index.commit()
    # node.logger.app_log.warning all token issuances

    # node.logger.app_log.warning("---")

    # node.logger.app_log.warning all transfers of a given token
    # token = "worthless"

    db_handler_instance.c.execute("SELECT operation, openfield FROM transactions WHERE (block_height >= ? OR block_height <= ?) AND operation = ? and reward = 0 ORDER BY block_height ASC;",
              (token_last_block, -token_last_block, "token:transfer",)) #includes mirror blocks
    openfield_transfers = db_handler_instance.c.fetchall()
    # print(openfield_transfers)

    tokens_transferred = []
    for transfer in openfield_transfers:
        token_name = transfer[1].split(":")[0].lower().strip()
        if token_name not in tokens_transferred:
            tokens_transferred.append(token_name)

    if tokens_transferred:
        node.logger.app_log.warning("Token transferred: {}".format(tokens_transferred))

    for token in tokens_transferred:
        try:
            node.logger.app_log.warning("processing {}".format(token))
            db_handler_instance.c.execute("SELECT block_height, timestamp, address, recipient, signature, operation, openfield FROM transactions WHERE (block_height >= ? OR block_height <= ?) AND operation = ? AND openfield LIKE ? AND reward = 0 ORDER BY block_height ASC;",
                      (token_last_block, -token_last_block, "token:transfer",token + ':%',))
            results2 = db_handler_instance.c.fetchall()
            node.logger.app_log.warning(results2)

            for r in results2:
                block_height = r[0]
                node.logger.app_log.warning("Block height {}".format(block_height))

                timestamp = r[1]
                node.logger.app_log.warning("Timestamp {}".format(timestamp))

                token = r[6].split(":")[0]
                node.logger.app_log.warning("Token {} operation".format(token))

                sender = r[2]
                node.logger.app_log.warning("Transfer from {}".format(sender))

                recipient = r[3]
                node.logger.app_log.warning("Transfer to {}".format(recipient))

                txid = r[4][:56]
                if txid == "0":
                    txid = blake2bhash_generate(r)
                node.logger.app_log.warning("Txid: {}".format(txid))

                try:
                    transfer_amount = int(r[6].split(":")[1])
                except:
                    transfer_amount = 0

                node.logger.app_log.warning("Transfer amount {}".format(transfer_amount))

                # calculate balances
                db_handler_instance.index_cursor.execute("SELECT sum(amount) FROM tokens WHERE recipient = ? AND block_height < ? AND token = ?",
                          (sender,block_height,token,))

                try:
                    credit_sender = int(db_handler_instance.index_cursor.fetchone()[0])
                except:
                    credit_sender = 0
                node.logger.app_log.warning("Sender's credit {}".format(credit_sender))

                db_handler_instance.index_cursor.execute("SELECT sum(amount) FROM tokens WHERE address = ? AND block_height <= ? AND token = ?",
                          (sender,block_height,token,))
                try:
                    debit_sender = int(db_handler_instance.index_cursor.fetchone()[0])
                except:
                    debit_sender = 0
                node.logger.app_log.warning("Sender's debit: {}".format(debit_sender))
                # calculate balances

                # node.logger.app_log.warning all token transfers
                balance_sender = credit_sender - debit_sender

                node.logger.app_log.warning("Sender's balance {}".format(balance_sender))

                try:
                    db_handler_instance.index_cursor.execute("SELECT txid from tokens WHERE txid = ?", (txid,))
                    dummy = db_handler_instance.index_cursor.fetchone()  # check for uniqueness
                    if dummy:
                        node.logger.app_log.warning("Token operation already processed: {} {}".format(token, txid))
                    else:
                        if (balance_sender - transfer_amount >= 0 and transfer_amount > 0):
                            db_handler_instance.index_cursor.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?)",
                                      (abs(block_height), timestamp, token, sender, recipient, txid, transfer_amount))
                            if node.plugin_manager:
                                node.plugin_manager.execute_action_hook('token_transfer',
                                                                   {'token': token, 'from': sender,
                                                                    'to': recipient, 'txid': txid, 'amount': transfer_amount})

                        else:  # save block height and txid so that we do not have to process the invalid transactions again
                            node.logger.app_log.warning("Invalid transaction by {}".format(sender))
                            db_handler_instance.index_cursor.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?)", (block_height, "", "", "", "", txid, ""))
                except Exception as e:
                    node.logger.app_log.warning("Exception {}".format(e))

                node.logger.app_log.warning("Processing of {} finished".format(token))
        except:
            node.logger.app_log.warning("Error parsing")

        db_handler_instance.index.commit()


if __name__ == "__main__":
    from libs import node,logger
    import dbhandler

    node = node.Node()
    node.debug_level = "WARNING"
    node.terminal_output = True

    node.logger = logger.Logger()
    node.logger.app_log = log.log("local_test.log", node.debug_level, node.terminal_output)
    node.logger.app_log.warning("Configuration settings loaded")

    db_handler = dbhandler.DbHandler("static/index_local_test.db","static/ledger.db","static/hyper.db", False, None, node.logger, False)

    tokens_update(node, db_handler)
    # tokens_update("tokens.db","reindex")
