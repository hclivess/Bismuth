#do not mine to the same address you use for staking, you will reduce your rewards

import sqlite3
import log

def delegates_list(file):
    """list masternode addresses registered for the period"""
    try:
        mas = sqlite3.connect(file)
        mas.text_factory = str
        m = mas.cursor()
        m.execute("SELECT DISTINCT delegate FROM masternodes")
        found = m.fetchall()[0]
        m.close()
    except:
        found = False

    return found



def stake_eligible(recipient, block_spread, reg_phase_start, reg_phase_end):
    """find out whether the masternode's delegate (or self) has staked in the past number of blocks decided by masternode_ratio"""
    conn = sqlite3.connect('static/ledger.db')
    conn.text_factory = str
    c = conn.cursor()

    c.execute("SELECT block_height FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
    block_last = c.fetchone()[0]

    try:
        delegates = delegates_list("static/index.db")

        c.execute("SELECT block_height FROM transactions WHERE reward != 0 AND block_height > ? AND block_height < ? AND address IN(?)",(reg_phase_start,)+(reg_phase_end,)+(delegates))
        last_staked = c.fetchone()[0]
    except:
        last_staked = 0

    try:
        c.execute("SELECT COUNT (*) FROM transactions WHERE recipient = ? reward != 0 AND block_height > ? AND block_height < ?",(recipient,)+(reg_phase_start,)+(reg_phase_end,))
        self_staked_count = c.fetchone()[0]
    except:
        self_staked_count = 0

    c.close()

    print("block_last",block_last)
    print("last_staked",last_staked)
    print("self_staked_count", self_staked_count)

    if (block_last - block_spread > last_staked):
        eligible = True
    else:
        eligible = False

    return eligible

def masternode_ratio(masternode_count):

    """report how many blocks a node can mine in the given phase and how often it can mine"""
    try:
        print ("masternode_count",masternode_count)
        ratio_blocks = int(1000 / masternode_count) #1000 blocks = 10% of mining during phase, result per single node
        block_spread = int(ratio_blocks / 100) #assume block per minute
    except:
        ratio_blocks, block_spread = 0, 0

    return ratio_blocks, block_spread

def masternode_count(file, app_log):
    """return the number of unique masternodes registered for the phase to determine reward ratio"""
    try:
        mas = sqlite3.connect(file)
        mas.text_factory = str
        m = mas.cursor()
        m.execute("SELECT COUNT(DISTINCT address) FROM masternodes")
        found = m.fetchone()[0]
        m.close()
    except:
        found = False

    return found

def masternode_find(file, address, app_log):
    """determine whether the masternode is registered for the phase"""
    mas = sqlite3.connect(file)
    mas.text_factory = str
    m = mas.cursor()
    m.execute("SELECT * FROM masternodes WHERE address = ?", (address,))
    found = m.fetchall()
    m.close()
    return found

def delegate_find(file, address, recipient, app_log):
    """find address of the delegate in case the delegate exists"""
    try:
        mas = sqlite3.connect(file)
        mas.text_factory = str
        m = mas.cursor()
        m.execute("SELECT address FROM masternodes WHERE delegate = ? AND address = ?", (recipient,) + (address,))
        found = m.fetchone()[0]
        m.close()
    except:
        found = False


    return found

def masternodes_update(file, mode, app_log):
    """update register of masternodes based on the current phase (10000 block intervals)"""
    if mode not in ("normal","reindex"):
        raise ValueError ("Wrong value for masternodes_update function")

    mas = sqlite3.connect(file)
    mas.text_factory = str
    m = mas.cursor()
    m.execute("CREATE TABLE IF NOT EXISTS masternodes (block_height INTEGER, timestamp NUMERIC, address, delegate, ip, txid)")
    mas.commit()

    if mode == "reindex":
        app_log.warning("Masternodes database will be reindexed")
        m.execute("DELETE FROM masternodes")
        mas.commit()

    conn = sqlite3.connect('static/ledger.db')
    conn.text_factory = str
    c = conn.cursor()

    c.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
    block_last = c.fetchone()[0] #get last block
    print ("block_last",block_last)

    i = 0

    reg_phase_end = block_last

    while True:
        if reg_phase_end % 10000 != 0:
            reg_phase_end = block_last - i
            i = i + 1
        else:
            break

    reg_phase_end = block_last#hack FOR TESTING ONLY

    reg_phase_start = reg_phase_end - 10000
    print("reg_phase_start", reg_phase_start)
    print("reg_phase_end", reg_phase_end)

    c.execute("SELECT block_height, timestamp, address, recipient, openfield, signature FROM transactions WHERE block_height >= ? AND block_height <= ? AND openfield LIKE ?", (reg_phase_start,) + (reg_phase_end,) + ("masternode:" + '%',))
    results = c.fetchall() #more efficient than "for row in"

    for row in results:
        block_height = row[0]
        timestamp = row[1]
        address = row[2]
        delegate = row[3]
        openfield_split = row[4].split(":")
        txid = row[5][:56]

        ip = openfield_split[1]
        print("openfield_split",openfield_split)
        print("delegate",delegate)
        print("ip",ip)

        try:
            m.execute("SELECT * from masternodes WHERE txid = ?", (txid,))
            dummy = m.fetchall()[0] #check for uniqueness
            app_log.warning("Masternode tx already registered: {}".format(txid))
        except:


            try:
                m.execute("SELECT * from masternodes WHERE address = ?", (address,))
                registration_requests = len(m.fetchall())
            except:
                registration_requests = 0

            print("registration_requests",registration_requests)

            if registration_requests > 3:
                app_log.warning("Masternode registration limit surpassed: {}".format(registration_requests))
            else:
                m.execute("INSERT INTO masternodes VALUES (?, ?, ?, ?, ?, ?)", (block_height, timestamp, address, delegate, ip, txid))
                mas.commit()

    c.close()
    m.close()

    return reg_phase_start, reg_phase_end

if __name__ == "__main__":
    address = "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed"
    delegate = "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed"

    app_log = log.log("masternodes.log", "WARNING", "yes")
    reg_phase_start, reg_phase_end = masternodes_update("static/index.db","normal",app_log)

    print(masternode_count("static/index.db", app_log))
    #print(masternode_find("static/index.db", address, app_log))
    print(delegate_find("static/index.db", address, delegate, app_log))
    print(masternode_ratio(masternode_count("static/index.db", app_log)))
    print(stake_eligible(delegate, masternode_ratio(masternode_count("static/index.db", app_log))[1],reg_phase_start,reg_phase_end))
    print(delegates_list("static/index.db"))
    #masternode:delegate:ip