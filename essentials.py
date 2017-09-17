import os, db, sqlite3

def db_check(app_log):
    if not os.path.exists('backup.db'):
        # create empty backup file
        backup = sqlite3.connect('backup.db', timeout=1)
        backup.text_factory = str
        b = backup.cursor()
        db.execute(b, ("CREATE TABLE IF NOT EXISTS transactions (block_height, timestamp, address, recipient, amount, signature, public_key, block_hash, fee, reward, keep, openfield)"), app_log)
        db.commit(backup, app_log)
        db.execute(b, ("CREATE TABLE IF NOT EXISTS misc (block_height, difficulty)"), app_log)
        db.commit(backup, app_log)
        app_log.warning("Created backup file")
        backup.close()
        # create empty backup file

    if not os.path.exists('mempool.db'):
        # create empty mempool
        mempool = sqlite3.connect('mempool.db', timeout=1)
        mempool.text_factory = str
        m = mempool.cursor()
        db.execute(m, ("CREATE TABLE IF NOT EXISTS transactions (timestamp, address, recipient, amount, signature, public_key, keep, openfield)"), app_log)
        db.commit(mempool, app_log)
        app_log.warning("Created mempool file")
        mempool.close()
        # create empty mempool