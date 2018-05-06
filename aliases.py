import sqlite3
import re
import log

# index aliases
def replace_regex(string,replace):
    replaced_string = re.sub(r'^{}'.format(replace), "", string)
    return replaced_string

def aliases_update(file,ledger,mode,app_log):
    if mode not in ("normal","reindex"):
        raise ValueError ("Wrong value for aliases_update function")

    conn = sqlite3.connect(ledger)
    conn.text_factory = str
    c = conn.cursor()

    ali = sqlite3.connect(file)
    ali.text_factory = str
    a = ali.cursor()
    a.execute("CREATE TABLE IF NOT EXISTS aliases (block_height INTEGER, address, alias)")
    ali.commit()

    if mode == "reindex":
        app_log.warning("Alias database will be reindexed")
        a.execute("DELETE FROM aliases")
        ali.commit()

    a.execute("SELECT block_height FROM aliases ORDER BY block_height DESC LIMIT 1;")
    try:
        alias_last_block = int(a.fetchone()[0])
    except:
        alias_last_block = 0

    app_log.warning("Alias anchor block: {}".format(alias_last_block))

    c.execute("SELECT block_height, address, openfield FROM transactions WHERE openfield LIKE ? AND block_height >= ? ORDER BY block_height ASC, timestamp ASC;", ("alias=" + '%',)+(alias_last_block,))
    #include the anchor block in case indexation stopped there
    result = c.fetchall()

    for openfield in result:
        alias = (replace_regex(openfield[2], "alias="))
        app_log.warning("Processing alias registration: {}".format(alias))
        try:
            a.execute("SELECT * from aliases WHERE alias = ?", (alias,))
            dummy = a.fetchall()[0] #check for uniqueness
            app_log.warning("Alias already registered: {}".format(alias))
        except:
            a.execute("INSERT INTO aliases VALUES (?,?,?)", (openfield[0],openfield[1],alias))
            ali.commit()

    conn.close()
    ali.close()

# index aliases

if __name__ == "__main__":
    app_log = log.log("tokens.log", "WARNING", True)
    aliases_update("static/index.db","static/ledger.db","normal",app_log)