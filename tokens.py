import sqlite3
import log

def tokens_update(file, mode, app_log):
    if mode not in ("normal","reindex"):
        raise ValueError ("Wrong value for tokens_update function")

    conn = sqlite3.connect('static/ledger.db')
    conn.text_factory = str
    c = conn.cursor()

    tok = sqlite3.connect(file)
    tok.text_factory = str
    t = tok.cursor()
    t.execute("CREATE TABLE IF NOT EXISTS tokens (block_height INTEGER, timestamp, token, address, recipient, txid, amount INTEGER)")
    tok.commit()

    if mode == "reindex":
        app_log.warning("Token database will be reindexed")
        t.execute("DELETE FROM tokens")
        tok.commit()

    t.execute("SELECT block_height FROM tokens ORDER BY block_height DESC LIMIT 1;")
    try:
        token_last_block = int(t.fetchone()[0])
    except:
        token_last_block = 0

    app_log.warning("Token anchor block: {}".format(token_last_block))

    # app_log.warning all token issuances
    c.execute("SELECT block_height, timestamp, address, recipient, signature, openfield FROM transactions WHERE block_height >= ? AND openfield LIKE ? AND reward = 0 ORDER BY block_height ASC;", (token_last_block,) + ("token:issue" + '%',))
    results = c.fetchall()
    #app_log.warning(results)

    tokens_processed = []

    for x in results:
        if x[5].split(":")[2].lower().strip() not in tokens_processed:
            block_height = x[0]
            app_log.warning("Block height {}".format(block_height))

            timestamp = x[1]
            app_log.warning("Timestamp {}".format(timestamp))

            token = x[5].split(":")[2].lower().strip()
            tokens_processed.append(token)
            app_log.warning("Token: {}".format(token))

            issued_by = x[3]
            app_log.warning("Issued by: {}".format(issued_by))

            txid = x[4][:56]
            app_log.warning("Txid: {}".format(txid))

            total = x[5].split(":")[3]
            app_log.warning("Total amount: {}".format(total))

            t.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?)", (block_height, timestamp, token, "issued", issued_by, txid, total))
        else:
            app_log.warning("Issuance already processed: {}".format(x[1]))

    tok.commit()
    # app_log.warning all token issuances

    #app_log.warning("---")

    # app_log.warning all transfers of a given token
    # token = "worthless"


    c.execute("SELECT openfield FROM transactions WHERE block_height >= ? AND openfield LIKE ? and reward = 0 ORDER BY block_height ASC;", (token_last_block,) + ("token:transfer" + '%',))
    openfield_transfers = c.fetchall()

    tokens_transferred = []
    for transfer in openfield_transfers:
        if transfer[0].split(":")[2].lower().strip() not in tokens_transferred:
            tokens_transferred.append(transfer[0].split(":")[2].lower().strip())

    if tokens_transferred:
        app_log.warning("Token transferred: {}".format(tokens_transferred))

    for token in tokens_transferred:
        app_log.warning("processing {}".format(token))
        c.execute("SELECT block_height, timestamp, address, recipient, signature, openfield FROM transactions WHERE block_height >= ? AND openfield LIKE ? AND reward = 0 ORDER BY block_height ASC;", (token_last_block,) + ("token:transfer:" + token + ':%',))
        results2 = c.fetchall()
        app_log.warning(results2)

        for r in results2:
            block_height = r[0]
            app_log.warning("Block height {}".format(block_height))

            timestamp = r[1]
            app_log.warning("Timestamp {}".format(timestamp))

            token = r[5].split(":")[2]
            app_log.warning("Token {} operation".format(token))

            sender = r[2]
            app_log.warning("Transfer from {}".format(sender))

            recipient = r[3]
            app_log.warning("Transfer to {}".format(recipient))

            txid = r[4][:56]
            app_log.warning("Txid: {}".format(txid))

            try:
                app_log.warning (r[5])
                transfer_amount = int(r[5].split(":")[3])
            except:
                transfer_amount = 0

            app_log.warning("Transfer amount {}".format(transfer_amount))

            # calculate balances
            t.execute("SELECT sum(amount) FROM tokens WHERE recipient = ? AND block_height < ? AND token = ?", (sender,) + (block_height,) + (token,))
            try:
                credit_sender = int(t.fetchone()[0])
            except:
                credit_sender = 0
            app_log.warning("Sender's credit {}".format(credit_sender))

            t.execute("SELECT sum(amount) FROM tokens WHERE address = ? AND block_height <= ? AND token = ?", (sender,) + (block_height,) + (token,))
            try:
                debit_sender = int(t.fetchone()[0])
            except:
                debit_sender = 0
            app_log.warning("Sender's debit: {}".format(debit_sender))
            # calculate balances

            # app_log.warning all token transfers
            balance_sender = credit_sender - debit_sender
            app_log.warning("Sender's balance {}".format(balance_sender))

            try:
                t.execute("SELECT * from tokens WHERE txid = ?", (txid,))
                dummy = t.fetchall()[0]  # check for uniqueness
                app_log.warning("Token operation already processed: {} {}".format(token, txid))
            except:
                if balance_sender - transfer_amount >= 0 and transfer_amount > 0:
                    t.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?)", (block_height, timestamp, token, sender, recipient, txid, transfer_amount))
                else: #save block height and txid so that we do not have to process the invalid transactions again
                    app_log.warning("Invalid transaction by {}".format(sender))
                    t.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?)", (block_height, "", "", "", "", txid, ""))

            app_log.warning("Processing of {} finished".format(token))

        tok.commit()

    tok.close()
    conn.close()


if __name__ == "__main__":
    app_log = log.log("tokens.log", "WARNING", "yes")
    tokens_update("index.db","normal",app_log)
    #tokens_update("tokens.db","reindex")
