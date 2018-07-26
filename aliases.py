import sqlite3
import re
import log

def replace_regex(string,replace):
    replaced_string = re.sub(r'^{}'.format(replace), "", string)
    return replaced_string

def aliases_update(f, ledger ,mode, app_log):
    """Where f is the aliases database file"""
    # index aliases
    if mode not in ("normal", "reindex"):
        raise ValueError ("Wrong value for aliases_update function")
    
    # removed `conn.text_factory = str` because sqlites default `text_factory` is `str`
    with sqlite3.connect(ledger) as conn:
        try:
            c = conn.cursor()
        except:
            app_log.error('Failed to create cursor for ledger')

        with sqlite3.connect(f) as ali:
            try:
                a = ali.cursor()
            except:
                app_log.error('Failed to create cursor for aliases')
                return

            try:
                a.execute("CREATE TABLE IF NOT EXISTS aliases (block_height INTEGER, address, alias)")
                ali.commit()
            except:
                app_log.error('Failed to create aliases table')
                return
            
            if mode == 'reindex':
                app_log.warning("Alias database will be reindexed")
                try:
                    a.execute("DELETE FROM aliases")
                    ali.commit()
                except:
                    app_log.error('Failed to delete content from aliases table')
                    return
            
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
                        app_log.warning("Added alias to the database: {} from block {}".format (alias,openfield[0]))


if __name__ == "__main__":
    app_log = log.log("aliases.log", "WARNING", True)
    aliases_update("static/index.db","static/ledger.db","normal",app_log)
