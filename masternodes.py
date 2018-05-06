#todo: node competition can be solved by comparing mn's hash (address) to block hash and picking the closest/most distant node to it as winner
#do not mine to the same address you use for staking, you will reduce your rewards

import sqlite3
import log
from difflib import SequenceMatcher


def delegates_list(file):
    """list masternode addresses registered for the period"""
    try:
        mas = sqlite3.connect(file)
        mas.text_factory = str
        m = mas.cursor()
        m.execute("SELECT DISTINCT delegate FROM masternodes")
        delegates_found = m.fetchall()
        m.close()
    except:
        delegates_found = False

    print ("delegates_found",delegates_found)
    return delegates_found


def candidate_select(delegates_list, hash_last):
    delegate_dict = {}

    for delegate in delegates_list:
        delegate = delegate[0] #drop tuple

        ratio = SequenceMatcher(None, delegate, hash_last).ratio()
        delegate_dict.update({delegate : ratio})
        print (delegate_dict)

    lowest_match =  min(delegate_dict, key=delegate_dict.get)
    print("lowest_match",lowest_match)
    return lowest_match


def stake_eligible(recipient, masternode_ratio, reg_phase_start, reg_phase_end):
    blocks_allowed = masternode_ratio[0]
    block_turn = masternode_ratio[1]


    """find out whether the masternode's delegate (or self) has staked in the past number of blocks decided by masternode_ratio and nobody else staked for x blocks"""
    conn = sqlite3.connect('static/ledger.db')
    conn.text_factory = str
    c = conn.cursor()

    c.execute("SELECT block_height FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
    block_last = c.fetchone()[0]

    try:
        delegates = delegates_list("static/index.db")

        #delegates = ("3d2e8fa99657ab59242f95ca09e0698a670e65c3ded951643c239bc7")#HACK TO TEST
        #recipient = ("3d2e8fa99657ab59242f95ca09e0698a670e65c3ded951643c239bc7")#HACK TO TEST

        c.execute("SELECT block_height, recipient FROM transactions WHERE reward != 0 AND block_height >= ? AND block_height <= ? ORDER BY block_height DESC",(reg_phase_start,)+(reg_phase_end,))
        mined = c.fetchall()
        for x in mined:
            if x[1] in delegates:
                last_staked = x[0]
                break
            else:
                last_staked = 0
    except:
        last_staked = 0

    try:
        c.execute("SELECT COUNT (*) FROM transactions WHERE recipient = ? AND reward != 0 AND block_height >= ? AND block_height <= ?",(recipient,)+(reg_phase_start,)+(reg_phase_end,))
        self_staked_count = c.fetchone()[0]
    except:
        self_staked_count = 0

    c.close()

    print("block_last",block_last)
    print("last_staked",last_staked)
    print("self_staked_count", self_staked_count)

    if (block_last - block_turn > last_staked) and (self_staked_count < blocks_allowed):
        eligible = True
    else:
        eligible = False

    return eligible

def masternode_ratio(masternode_count):
    #masternode_count = 1000 #HACK

    """report how many blocks a node can mine in the given phase and how often it can mine"""
    try:
        
        blocks_allowed = int(1000 / masternode_count) #every node can mint this amount of blocks per period
        block_turn = int(blocks_allowed * masternode_count) #one block every x turns
    except:
        blocks_allowed, block_turn = 0, 0


    print ("block_turn",block_turn)
    print ("blocks_allowed",blocks_allowed)
    return blocks_allowed, block_turn

def masternode_count(file):
    """return the number of unique masternodes registered for the phase to determine reward ratio"""
    try:
        mas = sqlite3.connect(file)
        mas.text_factory = str
        m = mas.cursor()
        m.execute("SELECT COUNT(DISTINCT address) FROM masternodes")
        masternodes_found = m.fetchone()[0]
        m.close()
    except:
        masternodes_found = False

    print("masternodes_found", masternodes_found)
    return masternodes_found

    

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
        delegate_found = m.fetchone()[0]
        m.close()
    except:
        delegate_found = False


    return delegate_found

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

    #reg_phase_end = block_last#hack FOR TESTING ONLY

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

    app_log = log.log("masternodes.log", "WARNING", True)
    reg_phase_start, reg_phase_end = masternodes_update("static/index.db","normal",app_log)

    masternode_count("static/index.db")
    #print(masternode_find("static/index.db", address, app_log))
    print(delegate_find("static/index.db", address, delegate, app_log))
    print(masternode_ratio(masternode_count("static/index.db")))
    print(stake_eligible(delegate, masternode_ratio(masternode_count("static/index.db")),reg_phase_start,reg_phase_end))
    delegates_list("static/index.db")
    #masternode:delegate:ip

    candidate_select(delegates_list("static/index.db"),"4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed")