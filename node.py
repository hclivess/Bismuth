# it's recommended to only use code from the "release" state, running master blob may have issues
# never remove the str() conversion in data evaluation or database inserts or you will debug for 14 days as signed types mismatch
# never change the type of database columns from TEXT to anything else
# if you raise in the server thread, the server will die and node will stop
# never use codecs, they are bugged and do not provide proper serialization
# must unify node and client now that connections parameters are function parameters
# if you have a block of data and want to insert it into sqlite, you must use a single "commit" for the whole batch, it's 100x faster
VERSION = "DEV"

from itertools import groupby
from operator import itemgetter
import shutil, socketserver, base64, hashlib, os, re, sqlite3, sys, threading, time, socks, log, options, connections, random, keys, math, requests, tarfile, essentials


from Crypto.Hash import SHA
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5

# load config
# global ban_threshold
global banlist
banlist = []
global hdd_block

db_lock = threading.Lock()
mem_lock = threading.Lock()
peersync_lock = threading.Lock()

config = options.Get()
config.read()

debug_level = config.debug_level_conf
port = config.port
genesis_conf = config.genesis_conf
verify_conf = config.verify_conf
thread_limit_conf = config.thread_limit_conf
rebuild_db_conf = config.rebuild_db_conf
debug_conf = config.debug_conf
node_ip_conf = config.node_ip_conf
purge_conf = config.purge_conf
pause_conf = config.pause_conf
ledger_path_conf = config.ledger_path_conf
hyper_path_conf = config.hyper_path_conf
hyper_recompress_conf = config.hyper_recompress_conf
ban_threshold = config.ban_threshold
tor_conf = config.tor_conf
debug_level_conf = config.debug_level_conf
allowed = config.allowed_conf
pool_ip_conf = config.pool_ip_conf
sync_conf = config.sync_conf
pool_percentage_conf = config.pool_percentage_conf
mining_threads_conf = config.mining_threads_conf
diff_recalc_conf = config.diff_recalc_conf
pool_conf = config.pool_conf
ram_conf = config.ram_conf
pool_address = config.pool_address_conf
version = config.version_conf
full_ledger = config.full_ledger_conf
reveal_address=config.reveal_address
accept_peers=config.accept_peers

if "testnet" in version: #overwrite for testnet
    port = 2829
    full_ledger = 0
    hyper_path_conf = "static/test.db"
    hyper_recompress_conf = 0
    peerlist = "peers_test.txt"
else:
    peerlist = "peers.txt"



# load config

def most_common(lst):
    return max(set(lst), key=lst.count)

def bootstrap():
    try:
        archive_path = ledger_path_conf + ".tar.gz"
        download_file("http://bismuth.cz/ledger.tar.gz", archive_path)

        tar = tarfile.open(archive_path)
        tar.extractall("static/")  # NOT COMPATIBLE WITH CUSTOM PATH CONFS
        tar.close()

    except:
        app_log.warning("Something went wrong during bootstrapping, aborted")
        raise


def check_integrity(database):
    # check ledger integrity
    ledger_check = sqlite3.connect(database)
    ledger_check.text_factory = str
    l = ledger_check.cursor()

    try:
        l.execute("PRAGMA table_info('transactions')")
        redownload = 0
    except:
        redownload = 1

    if len(l.fetchall()) != 12:
        app_log.warning("Integrity check on database failed, bootstrapping from the website")
        redownload = 1
    else:
        ledger_check.close()

    if redownload == 1:
        bootstrap()


def percentage(percent, whole):
    return float((percent * whole) / 100)


def download_file(url, filename):
    try:
        r = requests.get(url, stream=True)
        with open(filename, 'wb') as f:
            shutil.copyfileobj(r.raw, f)

        return filename
    except:
        raise


def db_to_drive(hdd, h, hdd2, h2):
    global hdd_block

    app_log.warning("Moving new data to HDD")

    if ram_conf == 1: #select RAM as source database
        source_db = sqlite3.connect('file::memory:?cache=shared', uri=True, timeout=1)
    else: #select hyper.db as source database
        source_db = sqlite3.connect(hyper_path_conf, timeout=1)

    source_db.text_factory = str
    sc = source_db.cursor()

    execute_param(sc, ("SELECT * FROM transactions WHERE block_height > ? ORDER BY block_height ASC"), (hdd_block,))
    result1 = sc.fetchall()

    if full_ledger == 1: #we want to save to ledger.db from hyper.db
        for x in result1:
            h.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9], x[10], x[11]))
        commit(hdd)

    if ram_conf == 1: #we want to save to hyper.db from RAM
        for x in result1:
            h2.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9], x[10], x[11]))
        commit(hdd2)

    execute_param(sc, ("SELECT * FROM misc WHERE block_height > ? ORDER BY block_height ASC"), (hdd_block,))
    result2 = sc.fetchall()

    if full_ledger == 1: #we want to save to ledger.db from hyper.db
        for x in result2:
            h.execute("INSERT INTO misc VALUES (?,?)", (x[0], x[1]))
        commit(hdd)

    if ram_conf == 1: #we want to save to hyper.db from RAM
        for x in result2:
            h2.execute("INSERT INTO misc VALUES (?,?)", (x[0], x[1]))
        commit(hdd2)

    # reward
    execute_param(sc, ('SELECT * FROM transactions WHERE address = "Development Reward" AND CAST(openfield AS INTEGER) > ?'), (hdd_block,))
    result3 = sc.fetchall()
    if full_ledger == 1: #we want to save to ledger.db from hyper.db
        for x in result3:
            h.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9], x[10], x[11]))
        commit(hdd)

    elif ram_conf == 1: #we want to save to hyper.db from RAM
        for x in result3:
            h2.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9], x[10], x[11]))
        commit(hdd2)
    # reward

    h2.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1")
    hdd_block = h2.fetchone()[0]

    app_log.warning("Ledger updated successfully")


def db_h_define():
    hdd = sqlite3.connect(ledger_path_conf, timeout=1)
    hdd.text_factory = str
    h = hdd.cursor()
    return hdd, h


def db_h2_define():
    hdd2 = sqlite3.connect(hyper_path_conf, timeout=1)
    hdd2.text_factory = str
    h2 = hdd2.cursor()
    return hdd2, h2


def db_c_define():
    global hdd_block

    try:
        if ram_conf == 1:
            conn = sqlite3.connect('file::memory:?cache=shared', uri=True, timeout=1)
        else:
            conn = sqlite3.connect(hyper_path_conf, uri=True, timeout=1)

        conn.text_factory = str
        c = conn.cursor()

    except Exception as e:
        app_log.info(e)

    return conn, c


def db_b_define():
    backup = sqlite3.connect('backup.db', timeout=1)
    backup.text_factory = str
    b = backup.cursor()
    return backup, b


def db_m_define():
    mempool = sqlite3.connect('mempool.db', timeout=1)
    mempool.text_factory = str
    m = mempool.cursor()
    return mempool, m


app_log = log.log("node.log", debug_level_conf)

app_log.warning("Configuration settings loaded")


def unban(peer_ip):
    global warning_list
    #global banlist

    # warning_list = [x for x in warning_list if x != peer_ip] #delete all
    # banlist = [x for x in banlist if x != peer_ip]

    if peer_ip in warning_list:
        warning_list.remove(peer_ip)
        app_log.warning("Removed a warning for {}".format(peer_ip))


def warning(sdef, ip, reason, count):
    global banlist
    global warning_list
    global ban_threshold

    for x in range(count):
        warning_list.append(ip)
    app_log.warning("Added {} warning(s) to {}: {} ({} / {})".format(count, ip, reason, warning_list.count(ip), ban_threshold))

    if warning_list.count(ip) >= ban_threshold:
        banlist.append(ip)
        sdef.close()
        app_log.warning("{} is banned, because {}".format(ip, reason))
        return "banned"


def ledger_convert(ledger_path_conf, hyper_path_conf):
    try:

        if os.path.exists(hyper_path_conf):

            if full_ledger == 1:
                # cross-integrity check
                hdd = sqlite3.connect(ledger_path_conf, timeout=1)
                hdd.text_factory = str
                h = hdd.cursor()
                h.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1")
                hdd_block_last = h.fetchone()[0]
                hdd.close()

                hdd2 = sqlite3.connect(hyper_path_conf, timeout=1)
                hdd2.text_factory = str
                h2 = hdd2.cursor()
                h2.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1")
                hdd2_block_last = h2.fetchone()[0]
                hdd2.close()
                # cross-integrity check

                if hdd_block_last == hdd2_block_last and hyper_recompress_conf == 1:  # cross-integrity check
                    ledger_path_conf = hyper_path_conf  # only valid within the function
                    app_log.warning("Recompressing hyperblocks (keeping full ledger)")
                    recompress = 1
                elif hdd_block_last == hdd2_block_last and hyper_recompress_conf == 0:
                    app_log.warning("Hyperblock recompression skipped")
                    recompress = 0
                else:
                    app_log.warning("Cross-integrity check failed, hyperblocks will be rebuilt from full ledger")
                    recompress = 1
            else:
                if hyper_recompress_conf == 1:
                    app_log.warning("Recompressing hyperblocks (without full ledger)")
                    recompress = 1
                else:
                    app_log.warning("Hyperblock recompression skipped")
                    recompress = 0
        else:
            app_log.warning("Compressing ledger to Hyperblocks")
            recompress = 1

        if recompress == 1:
            depth = 10000  # REWORK TO REFLECT TIME INSTEAD OF BLOCKS

            # if os.path.exists(ledger_path_conf + '.temp'):
            #    os.remove(ledger_path_conf + '.temp')

            if full_ledger == 1:
                shutil.copy(ledger_path_conf, ledger_path_conf + '.temp')
                hyper = sqlite3.connect(ledger_path_conf + '.temp')
            else:
                shutil.copy(hyper_path_conf, ledger_path_conf + '.temp')
                hyper = sqlite3.connect(ledger_path_conf + '.temp')

            hyper.text_factory = str
            hyp = hyper.cursor()

            end_balance = 0
            addresses = []

            hyp.execute("UPDATE transactions SET address = 'Hypoblock' WHERE address = 'Hyperblock'")

            hyp.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1;")
            db_block_height = hyp.fetchone()[0]

            for row in hyp.execute("SELECT * FROM transactions WHERE (block_height < ? AND keep = '0') ORDER BY block_height;",
                                   (str(int(db_block_height) - depth),)):
                db_address = row[2]
                db_recipient = row[3]
                addresses.append(db_address.strip())
                addresses.append(db_recipient.strip())

            unique_addressess = set(addresses)

            for x in set(unique_addressess):
                hyp.execute("SELECT sum(amount) FROM transactions WHERE (recipient = ? AND block_height < ?  AND keep = '0');", (x,) + (str(int(db_block_height) - depth),))
                credit = hyp.fetchone()[0]
                credit = 0 if credit is None else float('%.8f' % credit)

                hyp.execute("SELECT sum(amount),sum(fee),sum(reward) FROM transactions WHERE (address = ? AND block_height < ? AND keep = '0');", (x,) + (str(int(db_block_height) - depth),))
                result = hyp.fetchall()
                debit = result[0][0]
                debit = 0 if debit is None else float('%.8f' % debit)

                fees = result[0][1]
                fees = 0 if fees is None else float('%.8f' % fees)

                rewards = result[0][2]
                rewards = 0 if rewards is None else float('%.8f' % rewards)

                end_balance = float('%.8f' % (float(credit) - float(debit) - float(fees) + float(rewards)))
                # app_log.info("Address: "+ str(x))
                # app_log.info("Credit: " + str(credit))
                # app_log.info("Debit: " + str(debit))
                # app_log.info("Fees: " + str(fees))
                # app_log.info("Rewards: " + str(rewards))
                # app_log.info("Balance: " + str(end_balance))

                # test for keep positivity
                hyp.execute("SELECT block_height FROM transactions WHERE address OR recipient = ?", (x,))
                keep_is = 1
                try:
                    hyp.fetchone()[0]
                except:
                    keep_is = 0
                # test for keep positivity

                if end_balance > 0 or keep_is == 1:
                    timestamp = str(time.time())
                    hyp.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (db_block_height - depth - 1, timestamp, "Hyperblock", x, float(end_balance), "0", "0", "0", "0", "0",
                                                                                              "0", "0"))
            hyper.commit()  # EXPERIMENTALLY DEDENTED TO TAKE EVERYTHING AT ONCE

            hyp.execute("DELETE FROM transactions WHERE block_height < ? AND address != 'Hyperblock' AND keep = '0';", (str(int(db_block_height) - depth),))
            hyper.commit()

            hyp.execute("VACUUM")
            hyper.close()

            if os.path.exists(hyper_path_conf):
                os.remove(hyper_path_conf)  # remove the old hyperblocks

            os.rename(ledger_path_conf + '.temp', hyper_path_conf)

        if full_ledger == 0 and os.path.exists(ledger_path_conf) and "testnet" not in version:
            os.remove(ledger_path_conf)
            app_log.warning("Removed full ledger and only kept hyperblocks")

    except Exception as e:
        raise ValueError("There was an issue converting to Hyperblocks: {}".format(e))


def most_common(lst):
    return max(set(lst), key=lst.count)


def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)


def commit(cursor):
    """Secure commit for slow nodes"""
    while True:
        try:
            cursor.commit()
            break
        except Exception as e:
            app_log.warning("Database cursor: {}".format(cursor))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(random.uniform(1, 3))


def execute(cursor, query):
    """Secure execute for slow nodes"""
    while True:
        try:
            cursor.execute(query)
            break
        except Exception as e:
            app_log.warning("Database query: {} {}".format(cursor, query))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(random.uniform(1, 3))
    return cursor


def execute_param(cursor, query, param):
    """Secure execute w/ param for slow nodes"""
    while True:
        try:
            cursor.execute(query, param)
            break
        except Exception as e:
            app_log.warning("Database query: {} {} {}".format(cursor, query, param))
            app_log.warning("Database retry reason: {}".format(e))
            time.sleep(random.random())
    return cursor


def difficulty(c):
    execute(c, "SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1")
    result = c.fetchall()[0]
    timestamp_last = float(result[1])
    block_height = int(result[0])

    execute_param(c, ("SELECT block_height FROM transactions WHERE CAST(timestamp AS INTEGER) > ? AND reward != 0"), (timestamp_last - 86400,))  # 86400=24h
    blocks_per_1440 = len(c.fetchall())
    app_log.warning("Blocks per day: {}".format(blocks_per_1440))

    execute(c, ("SELECT difficulty FROM misc ORDER BY block_height DESC LIMIT 1"))
    diff_block_previous = float(c.fetchone()[0])

    try:
        log = math.log2(blocks_per_1440 / 1440)
    except:
        log = math.log2(0.5 / 1440)
        app_log.info("Difficulty exception triggered! This should not happen!")

    app_log.warning("Difficulty retargeting: {}".format(log))

    difficulty = float('%.13f' % (diff_block_previous + log))  # increase/decrease diff by a little

    time_now = time.time()


    if "testnet" in version or int(block_height) >= 340000:
        if time_now > timestamp_last + 120:  # if 2 minutes passed
            execute(c, ("SELECT difficulty FROM misc ORDER BY block_height DESC LIMIT 5"))
            diff_5 = c.fetchall()[0]
            diff_lowest_5 = float(min(diff_5))

            if diff_lowest_5 < difficulty:
                candidate = diff_lowest_5 #if lowest of last 5 is lower than calculated diff
            else:
                candidate = difficulty

            difficulty2 = float('%.13f' % percentage(99, candidate)) #candidate -1%
        else:
            difficulty2 = difficulty

        if difficulty < 70:
            difficulty = 70

        if difficulty2 < 70:
            difficulty2 = 70

    else:
        if time_now > timestamp_last + 300: #if 5 minutes have passed
            difficulty2 = float('%.13f' % percentage(95, difficulty))

        else:
            difficulty2 = difficulty

        if difficulty < 45:
            difficulty = 45

        if difficulty2 < 45:
            difficulty2 = 45


    app_log.warning("Difficulty: {} {}".format(difficulty, difficulty2))

    # return (float(50), float(50)) #TEST ONLY
    return (float(difficulty), float(difficulty2))


global connection_pool
connection_pool = []
global peer_ip_list
peer_ip_list = []
global consensus_blockheight_list
consensus_blockheight_list = []
global tried
tried = []
global consensus_percentage
consensus_percentage = ""
global warning_list
warning_list = []
global consensus
consensus = ""
global syncing
syncing = []


# port = 2829 now defined by config

def mempool_merge(data, peer_ip, c, mempool, m):
    if mem_lock.locked() == False:
        mem_lock.acquire()

        if not data:
            app_log.info("Mempool from {} was empty".format(peer_ip))
            mem_lock.release()
        else:
            app_log.info("Mempool merging started from {}".format(peer_ip))

            while db_lock.locked() == True:  # prevent transactions which are just being digested from being added to mempool
                app_log.info("Waiting for block digestion to finish before merging mempool")
                time.sleep(1)

            # merge mempool

            try:
                block_list = data

                for transaction in block_list:  # set means unique
                    mempool_timestamp = '%.2f' % float(transaction[0])
                    mempool_address = str(transaction[1][:56])
                    mempool_recipient = str(transaction[2][:56])
                    mempool_amount = '%.8f' % float(transaction[3])
                    mempool_signature_enc = str(transaction[4][:684])
                    mempool_public_key_hashed = str(transaction[5][:1068])
                    mempool_keep = str(transaction[6][:1])
                    mempool_openfield = str(transaction[7])

                    mempool_public_key = RSA.importKey(base64.b64decode(mempool_public_key_hashed))  # convert readable key to instance
                    mempool_signature_dec = base64.b64decode(mempool_signature_enc)

                    acceptable = 1

                    execute_param(m, ("SELECT * FROM transactions WHERE signature = ?;"), (mempool_signature_enc,))  # condition 1)
                    try:
                        dummy1 = m.fetchall()[0]
                        # app_log.info("That transaction is already in our mempool")
                        acceptable = 0
                        mempool_in = 1
                    except:
                        mempool_in = 0


                    # reject transactions which are already in the ledger
                    execute_param(c, ("SELECT * FROM transactions WHERE signature = ?;"), (mempool_signature_enc,))  # condition 2
                    try:
                        dummy2 = c.fetchall()[0]
                        # app_log.info("That transaction is already in our ledger")
                        # reject transactions which are already in the ledger
                        acceptable = 0
                        ledger_in = 1
                    except:
                        ledger_in = 0

                    if mempool_keep != "1" and mempool_keep != "0":
                        app_log.info = ("Mempool: Wrong keep value {}".format(mempool_keep))
                        acceptable = 0

                    if mempool_address != hashlib.sha224(base64.b64decode(mempool_public_key_hashed)).hexdigest():
                        app_log.info("Mempool: Attempt to spend from a wrong address")
                        acceptable = 0

                    if float(mempool_amount) < 0:
                        acceptable = 0
                        app_log.info("Mempool: Negative balance spend attempt")

                    if float(mempool_timestamp) > time.time() + 30:  # dont accept future txs
                        acceptable = 0

                    if float(mempool_timestamp) < time.time() - 82800:  # dont accept old txs, mempool needs to be harsher than ledger
                        acceptable = 0

                    if (mempool_in == 1) and (ledger_in == 1):  # remove from mempool if it's in both ledger and mempool already
                        try:
                            execute_param(m, ("DELETE FROM transactions WHERE signature = ?;"), (mempool_signature_enc,))
                            commit(mempool)
                            app_log.info("Mempool: Transaction deleted from our mempool")
                        except:  # experimental try and except
                            app_log.info("Mempool: Transaction was not present in the pool anymore")
                            pass  # continue to mempool finished message

                            # verify signatures and balances

                    # verify signature
                    verifier = PKCS1_v1_5.new(mempool_public_key)

                    hash = SHA.new(str((mempool_timestamp, mempool_address, mempool_recipient, mempool_amount, mempool_keep, mempool_openfield)).encode("utf-8"))
                    if not verifier.verify(hash, mempool_signature_dec):
                        acceptable = 0
                        app_log.info("Mempool: Wrong signature in mempool insert attempt: {}".format(transaction))

                    # verify signature

                    if acceptable == 1:

                        # verify balance
                        # app_log.info("Mempool: Verifying balance")
                        app_log.info("Mempool: Received address: {}".format(mempool_address))

                        # include mempool fees
                        execute_param(m, ("SELECT count(amount), sum(amount) FROM transactions WHERE address = ?;"), (mempool_address,))
                        result = m.fetchall()[0]
                        if result[1] != None:
                            debit_mempool = float('%.8f' % (float(result[1]) + float(result[1]) * 0.001 + int(result[0]) * 0.01))
                        else:
                            debit_mempool = 0

                        # include mempool fees

                        # include the new block

                        execute_param(c, ("SELECT sum(amount) FROM transactions WHERE recipient = ?;"), (mempool_address,))
                        credit_ledger = c.fetchone()[0]
                        credit_ledger = 0 if credit_ledger is None else float('%.8f' % credit_ledger)
                        credit = float(credit_ledger)

                        execute_param(c, ("SELECT sum(amount) FROM transactions WHERE address = ?;"), (mempool_address,))
                        debit_ledger = c.fetchone()[0]
                        debit_ledger = 0 if debit_ledger is None else float('%.8f' % debit_ledger)

                        debit = float(debit_ledger) + float(debit_mempool)

                        execute_param(c, ("SELECT sum(fee),sum(reward) FROM transactions WHERE address = ?;"), (mempool_address,))
                        result = c.fetchall()[0]
                        fees = result[0]
                        fees = 0 if fees is None else float('%.8f' % fees)

                        rewards = result[1]
                        rewards = 0 if rewards is None else float('%.8f' % rewards)

                        # app_log.info("Mempool: Total credit: " + str(credit))
                        # app_log.info("Mempool: Total debit: " + str(debit))
                        balance = float('%.8f' % (float(credit) - float(debit) - float(fees) + float(rewards) - float(mempool_amount)))
                        balance_pre = float('%.8f' % (float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)))
                        # app_log.info("Mempool: Projected transction address balance: " + str(balance))

                        fee = '%.8f' % float(0.01 + (float(len(mempool_openfield)) / 100000) + int(mempool_keep))  # 0.01 dust

                        time_now = time.time()
                        if float(mempool_timestamp) > float(time_now) + 30:
                            app_log.info("Mempool: Future transaction not allowed, timestamp {} minutes in the future".format((float(mempool_timestamp) - float(time_now)) / 60))

                        elif float(time_now) - 86400 > float(mempool_timestamp):
                            app_log.info("Mempool: Transaction older than 24h not allowed.")

                        elif float(mempool_amount) > float(balance_pre):
                            app_log.info("Mempool: Sending more than owned")

                        elif (float(balance)) - (float(fee)) < 0:  # removed +float(db_amount) because it is a part of the Inbound block
                            app_log.info("Mempool: Cannot afford to pay fees")
                        # verify signatures and balances
                        else:
                            execute_param(m, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", (str(mempool_timestamp), str(mempool_address), str(mempool_recipient), str(mempool_amount),
                                                                                                   str(mempool_signature_enc), str(mempool_public_key_hashed), str(mempool_keep), str(mempool_openfield)))
                            app_log.info("Mempool updated with a received transaction from {}".format(peer_ip))
                            commit(mempool)  # Save (commit) the changes

                            # merge mempool

                            # receive mempool

                            # app_log.info("Mempool: Finished with {} received transactions from {}".format(len(block_list),peer_ip))
            except:
                app_log.info("Mempool: Error processing")

                if debug_conf == 1:
                    raise
                else:
                    return
            finally:
                mem_lock.release()


def peers_get():
    peer_dict = {}
    with open(peerlist) as f:
        for line in f:
            try:
                line = re.sub("[\)\(\:\\n\'\s]", "", line)
                peer_dict[line.split(",")[0]] = line.split(",")[1]
            except Exception as e:
                app_log.warning("Skipping peerlist entry because of wrong format: {}".format(line))
    return peer_dict


def purge_old_peers():
    drop_peer_dict = []
    peer_dict = peers_get()

    for key, value in peer_dict.items():
        HOST = key
        # app_log.info(HOST)
        PORT = int(value)
        # app_log.info(PORT)

        try:
            s = socks.socksocket()
            s.settimeout(0.6)
            if tor_conf == 1:
                s.settimeout(5)
                s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
            # s.setblocking(0)
            s.connect((HOST, PORT))
            s.close()
        except:
            if purge_conf == 1 and "testnet" not in version:
                # remove from peerlist if not connectible
                drop_peer_dict.append(key)
                print("Removed formerly active peer {} {}".format(HOST, PORT))
            pass

    output = open(peerlist, 'w')
    for key, value in peer_dict.items():
        if key not in drop_peer_dict:
            output.write("('" + key + "', '" + value + "')\n")
    output.close()


def verify(c):
    try:
        # verify blockchain
        execute(c, ("SELECT Count(*) FROM transactions"))
        db_rows = c.fetchone()[0]
        app_log.warning("Total steps: {}".format(db_rows))

        # verify genesis
        execute(c, ("SELECT recipient FROM transactions ORDER BY block_height ASC LIMIT 1"))
        genesis = c.fetchone()[0]
        app_log.warning("Genesis: {}".format(genesis))
        if str(genesis) != genesis_conf:  # change this line to your genesis address if you want to clone
            app_log.info("Invalid genesis address")
            sys.exit(1)
        # verify genesis

        invalid = 0
        for row in execute(c, ('SELECT * FROM transactions WHERE block_height > 0 and ORDER BY block_height')):

            db_block_height = row[0]
            db_timestamp = '%.2f' % float(row[1])
            db_address = row[2]
            db_recipient = row[3]
            db_amount = row[4]
            db_signature_enc = row[5]
            db_public_key_hashed = row[6]
            db_public_key = RSA.importKey(base64.b64decode(db_public_key_hashed))
            db_keep = str(row[10])
            db_openfield = row[11]

            db_transaction = (str(db_timestamp), str(db_address), str(db_recipient), '%.8f' % float(db_amount), str(db_keep), str(db_openfield))

            db_signature_dec = base64.b64decode(db_signature_enc)
            verifier = PKCS1_v1_5.new(db_public_key)
            hash = SHA.new(str(db_transaction).encode("utf-8"))
            if verifier.verify(hash, db_signature_dec):
                pass
            else:
                app_log.warning("The following transaction is invalid: {}".format(row))
                invalid = invalid + 1
                if db_block_height == str(1):
                    app_log.warning("Your genesis signature is invalid, someone meddled with the database")
                    sys.exit(1)

        if invalid == 0:
            app_log.warning("All transacitons in the local ledger are valid")

    except sqlite3.Error as e:
        app_log.info("Error %s:" % e.args[0])
        sys.exit(1)


def blocknf(block_hash_delete, peer_ip, conn, c, hdd, h, hdd2, h2, backup, b):
    global hdd_block

    if db_lock.locked() == False:
        db_lock.acquire()
        try:
            execute(c, ('SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1'))
            results = c.fetchone()
            db_block_height = results[0]
            db_block_hash = results[7]

            if db_block_height < 2:
                app_log.info("Will not roll back this block")

            elif (db_block_hash != block_hash_delete):
                # print db_block_hash
                # print block_hash_delete
                app_log.info("We moved away from the block to rollback, skipping")

            else:
                # backup

                execute_param(c, ("SELECT * FROM transactions WHERE block_height >= ?;"), (str(db_block_height),))
                backup_data = c.fetchall()

                for x in backup_data:
                    execute_param(b, ("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?);"), (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9], x[10], x[11],))
                commit(backup)

                execute_param(c, ("SELECT * FROM misc WHERE block_height >= ?;"), (str(db_block_height),))
                backup_data = c.fetchall()

                for x in backup_data:
                    execute_param(b, ("INSERT INTO misc VALUES (?,?);"), (x[0], x[1],))
                commit(backup)
                # backup

                # delete followups
                execute_param(c, ("DELETE FROM transactions WHERE block_height >= ?;"), (str(db_block_height),))
                commit(conn)

                execute_param(c, ("DELETE FROM misc WHERE block_height >= ?;"), (str(db_block_height),))
                commit(conn)

                execute_param(c, ('DELETE FROM transactions WHERE address = "Development Reward" AND CAST(openfield AS INTEGER) >= ?'), (str(db_block_height),))
                commit(conn)

                app_log.warning("Node {} didn't find block {}({}), rolled back".format(peer_ip, db_block_height, db_block_hash))

                # roll back hdd too
                if full_ledger == 1: #rollback ledger.db
                    execute_param(h, ("DELETE FROM transactions WHERE block_height >= ?;"), (str(db_block_height),))
                    commit(hdd)
                    execute_param(h, ("DELETE FROM misc WHERE block_height >= ?;"), (str(db_block_height),))
                    commit(hdd)

                if ram_conf == 1: #rollback hyper.db
                    execute_param(h2, ("DELETE FROM transactions WHERE block_height >= ?;"), (str(db_block_height),))
                    commit(hdd2)
                    execute_param(h2, ("DELETE FROM misc WHERE block_height >= ?;"), (str(db_block_height),))
                    commit(hdd2)

                hdd_block = int(db_block_height) - 1
                # roll back hdd too

                # roll back reward too
                if full_ledger == 1: #rollback ledger.db
                    execute_param(h, ('DELETE FROM transactions WHERE address = "Development Reward" AND CAST(openfield AS INTEGER) >= ?'), (str(db_block_height),))
                    commit(hdd)

                if ram_conf == 1: #rollback hyper.db
                    execute_param(h2, ('DELETE FROM transactions WHERE address = "Development Reward" AND CAST(openfield AS INTEGER) >= ?'), (str(db_block_height),))
                    commit(hdd2)
                # roll back reward too

        except:
            pass
        finally:
            db_lock.release()

            # delete followups


def consensus_add(peer_ip, consensus_blockheight):
    try:
        global peer_ip_list
        global consensus_blockheight_list
        global consensus_percentage
        global consensus

        if peer_ip not in peer_ip_list:
            app_log.info("Adding {} to consensus peer list".format(peer_ip))
            peer_ip_list.append(peer_ip)
            app_log.info("Assigning {} to peer block height list".format(consensus_blockheight))
            consensus_blockheight_list.append(str(int(consensus_blockheight)))

        if peer_ip in peer_ip_list:
            consensus_index = peer_ip_list.index(peer_ip)  # get where in this list it is

            if consensus_blockheight_list[consensus_index] == (consensus_blockheight):
                app_log.info("Opinion of {} hasn't changed".format(peer_ip))

            else:
                del peer_ip_list[consensus_index]  # remove ip
                del consensus_blockheight_list[consensus_index]  # remove ip's opinion

                app_log.info("Updating {} in consensus".format(peer_ip))
                peer_ip_list.append(peer_ip)
                consensus_blockheight_list.append(int(consensus_blockheight))

        consensus = most_common(consensus_blockheight_list)

        consensus_percentage = (float(
            consensus_blockheight_list.count(consensus) / float(len(consensus_blockheight_list)))) * 100

        # app_log.info("Current Outbound connections: {}".format(len(connection_pool)))
        # app_log.info("Current block consensus: {} = {}%".format(consensus,consensus_percentage))

        return
    except Exception as e:
        app_log.info(e)
        raise


def consensus_remove(peer_ip):
    global peer_ip_list
    global consensus_blockheight_list
    try:
        app_log.info("Consensus opinion list: {}".format(consensus_blockheight_list))
        app_log.info("Will remove {} from consensus pool {}".format(peer_ip, peer_ip_list))
        consensus_index = peer_ip_list.index(peer_ip)
        peer_ip_list.remove(peer_ip)
        del consensus_blockheight_list[consensus_index]  # remove ip's opinion
    except:
        app_log.info("IP of {} not present in the consensus pool".format(peer_ip))
        pass


def manager(c, conn):
    global banlist
    peer_dict = peers_get()

    while True:
        # dict_keys = peer_dict.keys()
        # random.shuffle(peer_dict.items())

        variability = [] #should invariable peers not be saved to peerlist at all? in that case, move this where peers are saved
        del variability [:]
        variable = []
        del variable [:]

        for key, value in peer_dict.items():
            variability.append(key.split(".")[:-1])

        for x in variability:
            if variability.count(x) < 3:
                variable.append(".".join(x))

        for key, value in peer_dict.items():
            HOST = key
            # app_log.info(HOST)
            PORT = int(value)

            for x in variable:
                if x in HOST:
                    if "testnet" in version:
                        PORT = 2829

                    if threading.active_count() < thread_limit_conf and str(HOST + ":" + str(PORT)) not in tried and str(HOST + ":" + str(PORT)) not in connection_pool and str(HOST) not in banlist:
                        app_log.info("Will attempt to connect to {}:{}".format(HOST, PORT))
                        tried.append(HOST + ":" + str(PORT))
                        t = threading.Thread(target=worker, args=(HOST, PORT))  # threaded connectivity to nodes here
                        app_log.info("---Starting a client thread " + str(threading.currentThread()) + "---")
                        t.daemon = True
                        t.start()
                    # client thread handling

        if len(connection_pool) < 1:
            app_log.warning("Only {} connections active, resetting banlist".format(len(connection_pool)))
            del banlist[:]
            del warning_list[:]

        if len(connection_pool) < 10:
            app_log.warning("Only {} connections active, resetting the connection history".format(len(connection_pool)))
            del tried[:]

        if banlist:
            app_log.warning("Status: Banlist: {}".format(banlist))

        app_log.info("Status: Syncing nodes: {}".format(syncing))
        app_log.info("Status: Syncing nodes: {}/3".format(len(syncing)))
        app_log.warning("Status: Threads at {} / {}".format(threading.active_count(), thread_limit_conf))
        app_log.info("Status: Tried: {}".format(tried))
        app_log.info("Status: List of Outbound connections: {}".format(connection_pool))
        app_log.warning("Status: Number of Outbound connections: {}".format(len(connection_pool)))
        if consensus:  # once the consensus is filled
            app_log.warning("Status: Consensus: {} = {}%".format(consensus, consensus_percentage))
            app_log.warning("Status: Consensus IP list: {}".format(peer_ip_list))
            app_log.warning("Status: Consensus opinion list: {}".format(consensus_blockheight_list))
            app_log.warning("Status: Total number of nodes: {}".format(len(consensus_blockheight_list)))

        # last block
        execute(c, "SELECT timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")  # or it takes the first
        last_block_ago = float(c.fetchone()[0])
        app_log.warning("Status: Last block was generated {} minutes ago".format('%.2f' % ((time.time() - last_block_ago) / 60)))
        # last block

        # app_log.info(threading.enumerate() all threads)
        time.sleep(30)


def digest_block(data, sdef, peer_ip, conn, c, mempool, m, hdd, h, hdd2, h2, h3):
    global banlist, hdd_block

    if db_lock.locked() == False:
        db_lock.acquire()
        block_valid = 1  # init

        app_log.info("Digesting started from {}".format(peer_ip))
        try:
            block_list = data

            # reject block with duplicate transactions
            signature_list = []
            block_transactions = []

            for transaction_list in block_list:

                for r in transaction_list:  # sig 4
                    signature_list.append(r[4])

                    # reject block with transactions which are already in the ledger
                    execute_param(h3, ("SELECT block_height FROM transactions WHERE signature = ?;"), (r[4],))
                    try:
                        result = h3.fetchall()[0]
                        error_msg = "That transaction is already in our ledger, row {}".format(result[0])
                        block_valid = 0

                    except:
                        pass
                        # reject block with transactions which are already in the ledger

                if len(signature_list) != len(set(signature_list)):
                    error_msg = "There are duplicate transactions in this block, rejected"
                    block_valid = 0  # dont really need this one
                del signature_list[:]

                # reject block with duplicate transactions

                # previous block info
                execute(c, ("SELECT block_hash, block_height,timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
                result = c.fetchall()
                db_block_hash = result[0][0]
                db_block_height = result[0][1]
                db_timestamp_last = float(result[0][2])
                block_height_new = db_block_height + 1
                # previous block info

                transaction_list_converted = []  # makes sure all the data are properly converted as in the previous lines
                for transaction in transaction_list:
                    # verify signatures
                    received_timestamp = '%.2f' % float(transaction[0])
                    received_address = str(transaction[1][:56])
                    received_recipient = str(transaction[2][:56])
                    received_amount = '%.8f' % float(transaction[3])
                    received_signature_enc = str(transaction[4][:684])
                    received_public_key_hashed = str(transaction[5][:1068])
                    received_keep = str(transaction[6][:1])
                    received_openfield = str(transaction[7])

                    transaction_list_converted.append((received_timestamp, received_address, received_recipient, received_amount, received_signature_enc, received_public_key_hashed, received_keep, received_openfield))

                    received_public_key = RSA.importKey(base64.b64decode(received_public_key_hashed))  # convert readable key to instance
                    received_signature_dec = base64.b64decode(received_signature_enc)
                    verifier = PKCS1_v1_5.new(received_public_key)

                    hash = SHA.new(str((received_timestamp, received_address, received_recipient, received_amount, received_keep, received_openfield)).encode("utf-8"))
                    if not verifier.verify(hash, received_signature_dec):
                        error_msg = "Invalid signature"
                        # print(received_timestamp +"\n"+ received_address +"\n"+ received_recipient +"\n"+ received_amount +"\n"+ received_keep +"\n"+ received_openfield)
                        block_valid = 0
                    else:
                        app_log.info("Valid signature")

                    if received_keep != "1" and received_keep != "0":
                        block_valid = 0
                        # print (type(received_keep))
                        error_msg = "Wrong keep value {}".format(received_keep)

                    if float(received_amount) < 0:
                        block_valid = 0
                        error_msg = "Negative balance spend attempt"

                    if transaction != transaction_list[-1]:  # non-mining txs
                        if received_address != hashlib.sha224(base64.b64decode(received_public_key_hashed)).hexdigest():
                            error_msg = "Attempt to spend from a wrong address"
                            block_valid = 0

                    if transaction == transaction_list[-1]:  # recognize the last transaction as the mining reward transaction
                        block_timestamp = received_timestamp
                        nonce = received_openfield[:128]
                        miner_address = received_address


                    time_now = time.time()
                    if float(time_now) + 30 < float(received_timestamp):
                        error_msg = "Future transaction not allowed, timestamp {} minutes in the future".format((float(received_timestamp) - float(time_now)) / 60)
                        block_valid = 0
                    if float(db_timestamp_last) - 86400 > float(received_timestamp):
                        error_msg = "Transaction older than 24h not allowed."
                        block_valid = 0
                        # verify signatures

                # reject blocks older than latest block
                if float(block_timestamp) <= float(db_timestamp_last):
                    block_valid = 0
                    error_msg = "Block is older than the previous one, will be rejected"
                # reject blocks older than latest block

                # calculate difficulty

                diff = difficulty(c)

                # app_log.info("Transaction list: {}".format(transaction_list_converted))
                block_hash = hashlib.sha224((str(transaction_list_converted) + db_block_hash).encode("utf-8")).hexdigest()
                # app_log.info("Last block hash: {}".format(db_block_hash))
                app_log.info("Calculated block hash: {}".format(block_hash))
                # app_log.info("Nonce: {}".format(nonce))

                mining_hash = bin_convert(hashlib.sha224((miner_address + nonce + db_block_hash).encode("utf-8")).hexdigest())

                mining_condition = bin_convert(db_block_hash)[0:int(diff[0])]
                if mining_condition in mining_hash:  # simplified comparison, no backwards mining
                    app_log.info("Difficulty requirement satisfied for block {} from {}".format(block_height_new, peer_ip))
                    diff = diff[0]

                elif time_now > db_timestamp_last + 300:  # simplify after merging fork

                    mining_condition = bin_convert(db_block_hash)[0:int(diff[1])]
                    if mining_condition in mining_hash:  # simplified comparison, no backwards mining
                        app_log.info("Readjusted difficulty requirement satisfied for block {} from {}".format(block_height_new, peer_ip))
                        diff = diff[1]
                    else:
                        # app_log.info("Digest: Difficulty requirement not satisfied: " + bin_convert(miner_address) + " " + bin_convert(block_hash))
                        error_msg = "Readjusted difficulty too low for block {} from {}, should be at least {}".format(block_height_new, peer_ip, diff[1])
                        block_valid = 0


                else:
                    # app_log.info("Digest: Difficulty requirement not satisfied: " + bin_convert(miner_address) + " " + bin_convert(block_hash))
                    error_msg = "Difficulty too low for block {} from {}, should be at least {}".format(block_height_new, peer_ip, diff[0])
                    block_valid = 0

                    # print data
                    # print transaction_list
                # match difficulty

                fees_block = []

                if peer_ip in banlist:
                    block_valid = 0
                    error_msg = "Cannot accept blocks form a banned peer"

                if block_valid == 0:
                    app_log.warning("Check 1: A part of the block is invalid, rejected: {}".format(error_msg))
                    error_msg = ""
                    app_log.info("Check 1: Complete rejected data: {}".format(data))
                    if warning(sdef, peer_ip, "Check 1: rejected block",2) == "banned":
                        raise ValueError("{} banned".format(peer_ip))

                if block_valid == 1:
                    for transaction in transaction_list:
                        db_timestamp = '%.2f' % float(transaction[0])
                        db_address = str(transaction[1][:56])
                        db_recipient = str(transaction[2][:56])
                        db_amount = '%.8f' % float(transaction[3])
                        db_signature = str(transaction[4][:684])
                        db_public_key_hashed = str(transaction[5][:1068])
                        db_keep = str(transaction[6][:1])
                        db_openfield = str(transaction[7])

                        # print "sync this"
                        # print block_timestamp
                        # print transaction_list
                        # print db_block_hash
                        # print (str((block_timestamp,transaction_list,db_block_hash)))

                        # app_log.info("Digest: tx sig not found in the local ledger, proceeding to check before insert")

                        # app_log.info("Digest: Verifying balance")
                        # app_log.info("Digest: Received address: " + str(db_address))

                        # include the new block

                        # if float(db_amount) > 0: todo: only check balances if user is spending

                        block_credit = 0
                        block_debit = 0

                        for x in transaction_list:  # quite nasty, care not to overlap variables
                            if x[2] == db_address:
                                block_credit = float(block_credit) + float(x[3])
                            if x[1] == db_address:
                                block_debit = float(block_debit) + float(x[3])

                        # app_log.info("Digest: Inbound block credit: " + str(block_credit))
                        # app_log.info("Digest: Inbound block debit: " + str(block_debit))
                        # include the new block

                        execute_param(c, ("SELECT sum(amount) FROM transactions WHERE recipient = ?;"), (db_address,))
                        credit_ledger = c.fetchone()[0]
                        credit_ledger = 0 if credit_ledger is None else float('%.8f' % credit_ledger)
                        credit = float(credit_ledger) + float(block_credit)

                        execute_param(c, ("SELECT sum(amount) FROM transactions WHERE address = ?;"), (db_address,))
                        debit_ledger = c.fetchone()[0]
                        debit_ledger = 0 if debit_ledger is None else float('%.8f' % debit_ledger)
                        debit = float(debit_ledger) + float(block_debit)

                        execute_param(c, ("SELECT sum(fee),sum(reward) FROM transactions WHERE address = ?;"),
                                      (db_address,))

                        result = c.fetchall()[0]
                        fees = result[0]
                        rewards = result[1]

                        fees = 0 if fees is None else float('%.8f' % fees)
                        rewards = 0 if rewards is None else float('%.8f' % rewards)

                        # app_log.info("Digest: Total credit: " + str(credit))
                        # app_log.info("Digest: Total debit: " + str(debit))
                        balance_pre = float('%.8f' % (float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)))  # without projection
                        balance = float('%.8f' % (float(credit) - float(debit) - float(fees) + float(rewards)))
                        # app_log.info("Digest: Projected transction address balance: " + str(balance))

                        fee = '%.8f' % float(0.01 + (float(len(db_openfield)) / 100000) + int(db_keep))  # 0.01 dust

                        fees_block.append(float(fee))
                        # app_log.info("Fee: " + str(fee))


                        # decide reward

                        if transaction == transaction_list[-1]:

                            db_amount = 0  # prevent spending from another address, because mining txs allow delegation
                            if db_block_height <= 10000000:
                                mining_reward = 15 - (float(block_height_new) / float(1000000))  # one zero less
                            else:
                                mining_reward = 0

                            reward = '%.8f' % float(mining_reward + sum(fees_block[:-1]))
                            fee = 0
                        else:
                            reward = 0

                            # dont request a fee for mined block so new accounts can mine

                        if float(balance_pre) < float(db_amount):
                            error_msg = "Sending more than owned"
                            block_valid = 0

                        elif (float(balance)) - (float(fee)) < 0:
                            error_msg = "Cannot afford to pay fees"
                            block_valid = 0

                        else:
                            # append, but do not insert to ledger before whole block is validated, not that it takes already validated values (decimals, length)
                            app_log.info("Digest: Appending transaction back to block with {} transactions in it".format(len(block_transactions)))
                            block_transactions.append((block_height_new, db_timestamp, db_address, db_recipient, db_amount, db_signature, db_public_key_hashed, block_hash, fee, reward, db_keep, db_openfield))

                        try:
                            execute_param(m, ("DELETE FROM transactions WHERE signature = ?;"), (db_signature,))  # delete tx from mempool now that it is in the ledger
                            commit(mempool)
                            app_log.info("Digest: Removed processed transaction from the mempool")
                        except:
                            # tx was not in the local mempool
                            pass

                    # whole block validation
                    if block_valid == 0:
                        app_log.info("Check 2: A part of the block is invalid, rejected: {}".format(error_msg))
                        error_msg = ""
                        app_log.info("Check 2: Complete rejected block: {}".format(data))
                        if warning(sdef, peer_ip, "Check 2: rejected block",2) == "banned":
                            raise ValueError("{} banned".format(peer_ip))

                    if block_valid == 1:

                        # save diff
                        execute_param(c, "INSERT INTO misc VALUES (?, ?)", (block_height_new, diff))
                        commit(conn)
                        # save diff

                        for transaction in block_transactions:
                            execute_param(c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                                str(transaction[0]), str(transaction[1]),
                                str(transaction[2]), str(transaction[3]),
                                str(transaction[4]), str(transaction[5]),
                                str(transaction[6]), str(transaction[7]),
                                str(transaction[8]), str(transaction[9]),
                                str(transaction[10]), str(transaction[11])))
                            # secure commit for slow nodes
                            commit(conn)

                            # dev reward
                            if int(block_height_new) % 10 == 0:  # every 10 blocks
                                if transaction == block_transactions[-1]:  # put at the end
                                    execute_param(c, "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                                  ("0", str(time_now), "Development Reward", str(genesis_conf), str(mining_reward),
                                                   "0", "0", "0", "0", "0", "0", str(block_height_new)))
                                    commit(conn)
                            # dev reward

                        app_log.warning("Block {} valid and saved from {}".format(block_height_new, peer_ip))

                        del block_transactions[:]
                        if peer_ip in warning_list or peer_ip in banlist:
                            unban(peer_ip)

                        # whole block validation
        except Exception as e:
            app_log.warning(e)
            if warning(sdef, peer_ip, "Block processing failed", 10) == "banned":
                app_log.info("{} banned".format(peer_ip))

            if debug_conf == 1:
                raise  # major debug client
            else:
                pass

        finally:
            app_log.info("Digesting complete")
            if block_valid == 1 and (full_ledger == 1 or ram_conf == 1): #first case move stuff from hyper.db to ledger.db; second case move stuff from ram to both
                db_to_drive(hdd, h, hdd2, h2)
            db_lock.release()


if os.path.exists("fresh_sync"):
    app_log.warning("Fresh sync required, bootstrapping from the website")
    os.remove("fresh_sync")
    bootstrap()

check_integrity(hyper_path_conf)
essentials.keys_check(app_log)
essentials.db_check(app_log)

# import keys
# key = RSA.importKey(open('privkey.der').read())
# private_key_readable = str(key.exportKey())
public_key_readable = open('pubkey.der'.encode('utf-8')).read()

if (len(public_key_readable)) != 271 and (len(public_key_readable)) != 799:
    raise ValueError("Invalid public key length: {}".format(len(public_key_readable)))

public_key_hashed = base64.b64encode(public_key_readable.encode('utf-8'))
address = hashlib.sha224(public_key_readable.encode('utf-8')).hexdigest()

app_log.warning("Local address: {}".format(address))

# check if mempool needs recreating
mempool = sqlite3.connect('mempool.db', timeout=1)
mempool.text_factory = str
m = mempool.cursor()
m.execute("PRAGMA table_info('transactions')")
if len(m.fetchall()) != 8:
    mempool.close()
    os.remove("mempool.db")
    mempool = sqlite3.connect('mempool.db', timeout=1)
    mempool.text_factory = str
    m = mempool.cursor()
    execute(m, ("CREATE TABLE IF NOT EXISTS transactions (timestamp, address, recipient, amount, signature, public_key, keep, openfield)"))
    commit(mempool)
    app_log.info("Recreated mempool file")


# check if mempool needs recreating

def coherence_check():
    app_log.warning("Testing chain coherence")
    if full_ledger == 1:
        chains_to_check = [ledger_path_conf, hyper_path_conf]
    else:
        chains_to_check = [hyper_path_conf]

    for chain in chains_to_check:
        conn = sqlite3.connect(chain)
        c = conn.cursor()

        c.execute("SELECT block_height FROM transactions WHERE reward != 0 AND block_height != (0 OR 1) ORDER BY block_height ASC")
        result = c.fetchall()

        my_list = []
        for x in result:
            my_list.append(x[0])

        y = my_list[0] - 1
        coherent = 1
        for x in my_list:
            if x != y + 1:
                app_log.warning("Chain {} coherence error at: {}".format(chain, y))
                coherent = 0
            y = x

        if coherent == 1:
            app_log.warning("Chain {} is coherent".format(chain))
        conn.close()


coherence_check()

ledger_convert(ledger_path_conf, hyper_path_conf)


try:
    source_db = sqlite3.connect(hyper_path_conf, timeout=1)
    source_db.text_factory = str
    sc = source_db.cursor()

    sc.execute("SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1")
    hdd_block = sc.fetchone()[0]

    if ram_conf == 1:

        app_log.warning("Moving database to RAM")
        to_ram = sqlite3.connect('file::memory:?cache=shared', uri=True, timeout=1)
        to_ram.text_factory = str
        tr = to_ram.cursor()

        query = "".join(line for line in source_db.iterdump())
        to_ram.executescript(query)
        #do not close
        app_log.warning("Moved database to RAM")

except Exception as e:
    app_log.info(e)

backup, b = db_b_define()
mempool, m = db_m_define()
conn, c = db_c_define()
hdd2, h2 = db_h2_define()
if full_ledger == 1:
    hdd, h = db_h_define()
    h3 = h
else:
    hdd, h = None, None
    h3 = h2


# init
def db_maintenance():
    # db maintenance
    execute(conn, "VACUUM")
    execute(mempool, "VACUUM")
    app_log.warning("Database maintenance finished")


if rebuild_db_conf == 1:
    db_maintenance()
# connectivity to self node

if verify_conf == 1:
    verify(c)

# init

### LOCAL CHECKS FINISHED ###
app_log.warning("Starting...")
global startup_time
startup_time = time.time()

class ThreadedTCPRequestHandler(socketserver.BaseRequestHandler):
    def handle(self):  # server defined here
        global banlist
        global ban_threshold

        peer_ip = self.request.getpeername()[0]

        if threading.active_count() < thread_limit_conf or peer_ip == "127.0.0.1":
            capacity = 1
        else:
            capacity = 0
            self.request.close()
            app_log.info("Free capacity for {} unavailable, disconnected".format(peer_ip))
            # if you raise here, you kill the whole server

        if peer_ip not in banlist:
            banned = 0
        else:
            banned = 1
            self.request.close()
            app_log.info("IP {} banned, disconnected".format(peer_ip))

            # if you raise here, you kill the whole server

        timeout_operation = 120  # timeout
        timer_operation = time.time()  # start counting

        while banned == 0 and capacity == 1:
            try:
                hdd2, h2 = db_h2_define()
                backup, b = db_b_define()
                mempool, m = db_m_define()
                conn, c = db_c_define()
                if full_ledger == 1:
                    hdd, h = db_h_define()
                    h3 = h
                else:
                    hdd, h = None,None
                    h3 = h2


                if not time.time() <= timer_operation + timeout_operation:  # return on timeout
                    if warning(self.request, peer_ip, "Operation timeout", 2) == "banned":
                        app_log.info("{} banned".format(peer_ip))
                        break

                    raise ValueError("Inbound: Operation timeout from {}".format(peer_ip))

                data = connections.receive(self.request, 10)

                app_log.info("Inbound: Received: {} from {}".format(data, peer_ip))  # will add custom ports later

                if data == 'version':
                    data = connections.receive(self.request, 10)
                    if version != data:
                        app_log.info("Protocol version mismatch: {}, should be {}".format(data, version))
                        connections.send(self.request, "notok", 10)
                        return
                    else:
                        app_log.info("Inbound: Protocol version matched: {}".format(data))
                        connections.send(self.request, "ok", 10)

                elif data == 'mempool':

                    # receive theirs
                    segments = connections.receive(self.request, 10)

                    mempool_merge(segments, peer_ip, c, mempool, m)
                    # receive theirs

                    execute(m, ('SELECT * FROM transactions'))
                    mempool_txs = m.fetchall()

                    # send own
                    # app_log.info("Inbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: same as the other
                    connections.send(self.request, mempool_txs, 10)
                    # send own

                elif data == 'hello':
                    with open(peerlist, "r") as peer_list:
                        peers = peer_list.read()

                        connections.send(self.request, "peers", 10)
                        connections.send(self.request, peers, 10)

                    peer_list.close()

                    # save peer if connectible
                    if accept_peers == "yes":
                        peer_file = open(peerlist, 'r')
                        peer_tuples = []
                        for line in peer_file:
                            extension = re.findall("'([\d\.]+)', '([\d]+)'", line)
                            peer_tuples.extend(extension)
                        peer_file.close()
                        peer_tuple = ("('" + peer_ip + "', '" + str(port) + "')")

                        try:
                            app_log.warning("Testing connectivity to: {}".format(peer_ip))
                            peer_test = socks.socksocket()
                            if tor_conf == 1:
                                peer_test.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                            # peer_test.setblocking(0)
                            peer_test.connect((str(peer_ip), int(str(port))))  # double parentheses mean tuple
                            app_log.info("Inbound: Distant peer connectible")

                            # properly end the connection
                            peer_test.close()
                            # properly end the connection
                            if peer_tuple not in str(peer_tuples):
                                peer_list_file = open(peerlist, 'a')
                                peer_list_file.write((peer_tuple) + "\n")
                                app_log.info("Inbound: Distant peer saved to peer list")
                                peer_list_file.close()
                            else:
                                app_log.info("Distant peer already in peer list")
                        except:
                            app_log.info("Inbound: Distant peer not connectible")
                            pass

                            # raise #test only

                    # save peer if connectible

                    while db_lock.locked() == True:
                        time.sleep(float(pause_conf))
                    app_log.info("Inbound: Sending sync request")

                    connections.send(self.request, "sync", 10)

                elif data == "sendsync":
                    while db_lock.locked() == True:
                        time.sleep(float(pause_conf))

                    global syncing
                    while len(syncing) >= 3:
                        time.sleep(int(pause_conf))

                    connections.send(self.request, "sync", 10)


                elif data == "blocksfnd":
                    app_log.info("Inbound: Client {} has the block(s)".format(peer_ip))  # node should start sending txs in this step

                    # app_log.info("Inbound: Combined segments: " + segments)
                    # print peer_ip
                    if db_lock.locked() == True:
                        app_log.info("Skipping sync from {}, syncing already in progress".format(peer_ip))

                    else:
                        execute(c, "SELECT timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")  # or it takes the first
                        last_block_ago = float(c.fetchone()[0])

                        if int(last_block_ago) < (time.time() - 600):
                            block_req = most_common(consensus_blockheight_list)
                            app_log.warning("Most common block rule triggered")
                        else:
                            block_req = max(consensus_blockheight_list)
                            app_log.warning("Longest chain rule triggered")

                        if int(received_block_height) >= block_req:
                            app_log.warning("Confirming to sync from {}".format(peer_ip))

                            try: #they claim to have the longest chain, things must go smooth or ban
                                connections.send(self.request, "blockscf", 10)
                                segments = connections.receive(self.request, 10)
                            except:
                                if warning(self.request, peer_ip, "Failed to deliver the longest chain", 10) == "banned":
                                    app_log.info("{} banned".format(peer_ip))
                                    break

                            if db_lock.locked() == False: #second check for lock
                                digest_block(segments, self.request, peer_ip, conn, c, mempool, m, hdd, h, hdd2, h2, h3)
                            else:
                                app_log.warning("Skipping block processing from {}, someone delivered data faster".format(peer_ip))
                            # receive theirs

                        else:
                            app_log.warning("Rejecting to sync from {}".format(peer_ip))
                            connections.send(self.request, "blocksrj", 10)
                            app_log.info("Inbound: Distant peer {} is at {}, should be at least {}".format(peer_ip, received_block_height, block_req))

                    connections.send(self.request, "sync", 10)

                elif data == "blockheight":
                    try:
                        received_block_height = connections.receive(self.request, 10)  # receive client's last block height
                        app_log.info("Inbound: Received block height {} from {} ".format(received_block_height, peer_ip))

                        # consensus pool 1 (connection from them)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        consensus_add(peer_ip, consensus_blockheight)
                        # consensus pool 1 (connection from them)


                        execute(c, ('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1'))
                        db_block_height = c.fetchone()[0]

                        # append zeroes to get static length
                        connections.send(self.request, db_block_height, 10)
                        # send own block height

                        if int(received_block_height) > db_block_height:
                            app_log.warning("Inbound: Client has higher block")

                            execute(c, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                            db_block_hash = c.fetchone()[0]  # get latest block_hash

                            app_log.info("Inbound: block_hash to send: " + str(db_block_hash))
                            connections.send(self.request, db_block_hash, 10)

                            # receive their latest hash
                            # confirm you know that hash or continue receiving

                        elif int(received_block_height) <= db_block_height:
                            if int(received_block_height) == db_block_height:
                                app_log.info("Inbound: We have the same height as {} ({}), hash will be verified".format(peer_ip, received_block_height))
                            else:
                                app_log.warning("Inbound: We have higher ({}) block height than {} ({}), hash will be verified".format(db_block_height, peer_ip, received_block_height))

                            data = connections.receive(self.request, 10)  # receive client's last block_hash
                            # send all our followup hashes

                            app_log.info("Inbound: Will seek the following block: {}".format(data))

                            try:
                                execute_param(h3, ("SELECT block_height FROM transactions WHERE block_hash = ?;"), (data,))
                                client_block = h3.fetchone()[0]

                                app_log.info("Inbound: Client is at block {}".format(client_block))  # now check if we have any newer

                                execute(h3, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                                db_block_hash = h3.fetchone()[0]  # get latest block_hash
                                if db_block_hash == data:
                                    app_log.info("Inbound: Client has the latest block")
                                    connections.send(self.request, "nonewblk", 10)

                                else:
                                    execute_param(h3, ("SELECT block_height, timestamp,address,recipient,amount,signature,public_key,keep,openfield FROM transactions WHERE block_height > ? AND block_height < ?;"),
                                                  (str(int(client_block)),) + (str(int(client_block + 1500)),))  # select Inbound transaction + 1
                                    blocks_fetched = h3.fetchall()

                                    blocks_send = [[l[1:] for l in group] for _, group in groupby(blocks_fetched, key=itemgetter(0))]  # remove block number

                                    # app_log.info("Inbound: Selected " + str(blocks_send) + " to send")


                                    connections.send(self.request, "blocksfnd", 10)

                                    confirmation = connections.receive(self.request, 10)

                                    if confirmation == "blockscf":
                                        app_log.info("Inbound: Client confirmed they want to sync from us")
                                        connections.send(self.request, blocks_send, 10)

                                    elif confirmation == "blocksrj":
                                        app_log.info("Inbound: Client rejected to sync from us because we're don't have the latest block")
                                        pass

                                        # send own

                            except:
                                app_log.info("Inbound: Block not found")
                                connections.send(self.request, "blocknf", 10)
                                connections.send(self.request, data, 10)
                    except Exception as e:
                        app_log.info("Inbound: Sync failed {}".format(e))


                elif data == "nonewblk":
                    connections.send(self.request, "sync", 10)

                elif data == "blocknf":
                    block_hash_delete = connections.receive(self.request, 10)
                    # print peer_ip
                    if max(consensus_blockheight_list) == consensus_blockheight:
                        blocknf(block_hash_delete, peer_ip, conn, c, hdd, h, hdd2, h2, backup, b)
                        if warning(self.request, peer_ip, "Rollback",2) == "banned":
                            app_log.info("{} banned".format(peer_ip))
                            break
                    app_log.info("Outbound: Deletion complete, sending sync request")

                    while db_lock.locked() == True:
                        time.sleep(float(pause_conf))
                    connections.send(self.request, "sync", 10)

                elif data == "block":
                    if (peer_ip in allowed or "any" in allowed):  # from miner

                        app_log.info("Outbound: Received a block from miner {}".format(peer_ip))
                        # receive block
                        segments = connections.receive(self.request, 10)
                        # app_log.info("Inbound: Combined mined segments: " + segments)

                        # check if we have the latest block

                        execute(c, ('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1'))
                        db_block_height = c.fetchone()[0]

                        # check if we have the latest block

                        if "testnet" not in version:
                            if len(connection_pool) < 5:
                                app_log.info("Outbound: Mined block ignored, insufficient connections to the network")
                            elif int(db_block_height) >= int(max(consensus_blockheight_list)) - 3 and db_lock.locked() == False:
                                app_log.info("Outbound: Processing block from miner")
                                digest_block(segments, self.request, peer_ip, conn, c, mempool, m, hdd, h, hdd2, h2, h3)
                            elif db_lock.locked() == True:
                                app_log.info("Outbound: Block from miner skipped because we are digesting already")

                            # receive theirs
                            else:
                                app_log.info("Outbound: Mined block was orphaned because node was not synced, we are at block {}, should be at least {}".format(db_block_height, max(consensus_blockheight_list) - 3))
                        else:
                            digest_block(segments, self.request, peer_ip, conn, c, mempool, m, hdd, h, hdd2, h2, h3)
                    else:
                        connections.receive(self.request, 10)  # receive block, but do nothing about it
                        app_log.info("{} not whitelisted for block command".format(peer_ip))

                elif data == "blocklast":
                    if (peer_ip in allowed or "any" in allowed):  # only sends the miner part of the block!

                        execute(c, ("SELECT * FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;"))
                        block_last = c.fetchall()[0]

                        connections.send(self.request, block_last, 10)
                    else:
                        app_log.info("{} not whitelisted for blocklast command".format(peer_ip))

                elif data == "blockget":
                    if (peer_ip in allowed or "any" in allowed):
                        block_desired = connections.receive(self.request, 10)

                        execute_param(h3, ("SELECT * FROM transactions WHERE block_height = ?;"), (block_desired,))
                        block_desired_result = h3.fetchall()

                        connections.send(self.request, block_desired_result, 10)
                    else:
                        app_log.info("{} not whitelisted for blockget command".format(peer_ip))

                elif data == "mpinsert":
                    if (peer_ip in allowed or "any" in allowed):
                        mempool_insert = connections.receive(self.request, 10)
                        mempool_merge(mempool_insert, peer_ip, c, mempool, m)
                        connections.send(self.request, "Mempool insert finished", 10)
                    else:
                        app_log.info("{} not whitelisted for mpinsert command".format(peer_ip))

                elif data == "balanceget":
                    if (peer_ip in allowed or "any" in allowed):
                        balance_address = connections.receive(self.request, 10)  # for which address

                        # verify balance

                        # app_log.info("Mempool: Verifying balance")
                        # app_log.info("Mempool: Received address: " + str(balance_address))

                        # include mempool fees
                        execute_param(m, ("SELECT count(amount), sum(amount) FROM transactions WHERE address = ?;"), (balance_address,))
                        result = m.fetchall()[0]
                        if result[1] != None:
                            debit_mempool = float('%.8f' % (float(result[1]) + float(result[1]) * 0.001 + int(result[0]) * 0.01))
                        else:
                            debit_mempool = 0

                        # include mempool fees

                        execute_param(h3, ("SELECT sum(amount) FROM transactions WHERE recipient = ?;"), (balance_address,))
                        credit_ledger = h3.fetchone()[0]
                        credit_ledger = 0 if credit_ledger is None else float('%.8f' % credit_ledger)
                        credit = float(credit_ledger)

                        execute_param(h3, ("SELECT sum(fee),sum(reward),sum(amount) FROM transactions WHERE address = ?;"), (balance_address,))
                        result = h3.fetchall()[0]

                        fees = result[0]
                        fees = 0 if fees is None else float('%.8f' % fees)

                        rewards = result[1]
                        rewards = 0 if rewards is None else float('%.8f' % rewards)

                        debit_ledger = result[2]
                        debit_ledger = 0 if debit_ledger is None else float('%.8f' % debit_ledger)

                        debit = float(debit_ledger) + float(debit_mempool)

                        balance = float('%.8f' % (float(credit) - float(debit) - float(fees) + float(rewards)))
                        # balance_pre = float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)
                        # app_log.info("Mempool: Projected transction address balance: " + str(balance))

                        connections.send(self.request, (balance, credit, debit, fees, rewards), 10)  # return balance of the address to the client, including mempool
                        # connections.send(self.request, balance_pre, 10)  # return balance of the address to the client, no mempool
                    else:
                        app_log.info("{} not whitelisted for balanceget command".format(peer_ip))

                elif data == "mpget" and (peer_ip in allowed or "any" in allowed):
                    execute(m, ('SELECT * FROM transactions'))
                    mempool_txs = m.fetchall()

                    # app_log.info("Outbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                    # send own
                    connections.send(self.request, mempool_txs, 10)

                elif data == "keygen":
                    if (peer_ip in allowed or "any" in allowed):
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = keys.generate()
                        connections.send(self.request, (gen_private_key_readable, gen_public_key_readable, gen_address), 10)
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = (None, None, None)
                    else:
                        app_log.info("{} not whitelisted for keygen command".format(peer_ip))

                elif data == "addlist":
                    if (peer_ip in allowed or "any" in allowed):
                        address_tx_list = connections.receive(self.request, 10)
                        execute_param(h3, ("SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC"), (address_tx_list,) + (address_tx_list,))
                        result = h3.fetchall()
                        connections.send(self.request, result, 10)
                    else:
                        app_log.info("{} not whitelisted for addlist command".format(peer_ip))

                elif data == "addlistlim":
                    if (peer_ip in allowed or "any" in allowed):
                        address_tx_list = connections.receive(self.request, 10)
                        address_tx_list_limit = connections.receive(self.request, 10)
                        #print (address_tx_list_limit)
                        execute_param(h3, ("SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC LIMIT ?"), (address_tx_list,) + (address_tx_list,) + (address_tx_list_limit,))
                        result = h3.fetchall()
                        connections.send(self.request, result, 10)
                    else:
                        app_log.info("{} not whitelisted for addlist command".format(peer_ip))

                elif data == "aliasget":
                    if (peer_ip in allowed or "any" in allowed):
                        alias_address = connections.receive(self.request, 10)

                        execute_param(h3, ("SELECT openfield FROM transactions WHERE address = ? AND openfield LIKE ?;"), (alias_address,) + ("alias=" + '%',))

                        result = h3.fetchall()

                        if not result:
                            result = [[alias_address]]

                        connections.send(self.request, result, 10)
                    else:
                        app_log.info("{} not whitelisted for aliasget command".format(peer_ip))


                elif data == "pubkeyget":
                    if (peer_ip in allowed or "any" in allowed):
                        pub_key_address = connections.receive(self.request, 10)

                        c.execute("SELECT public_key FROM transactions WHERE address = ? and reward = 0", (pub_key_address,))
                        target_public_key_hashed = c.fetchone()[0]
                        connections.send(self.request, target_public_key_hashed, 10)

                    else:
                        app_log.info("{} not whitelisted for pubkeyget command".format(peer_ip))

                elif data == "aliascheck":
                    if (peer_ip in allowed or "any" in allowed):
                        reg_string = connections.receive(self.request, 10)

                        m.execute("SELECT timestamp FROM transactions WHERE openfield = ?;", ("alias=" + reg_string,))
                        registered_pending = m.fetchone()

                        h3.execute("SELECT timestamp FROM transactions WHERE openfield = ?;", ("alias=" + reg_string,))
                        registered_already = h3.fetchone()

                        if registered_already is None and registered_pending is None:
                            connections.send(self.request, "Alias free", 10)
                        else:
                            connections.send(self.request, "Alias registered", 10)
                    else:
                        app_log.info("{} not whitelisted for aliascheck command".format(peer_ip))

                elif data == "aliasesget": #only gets the first one
                    if (peer_ip in allowed or "any" in allowed):
                        alias_addresses = connections.receive(self.request, 10)

                        results=[]
                        for x in alias_addresses:
                            execute_param(h3, ("SELECT openfield FROM transactions WHERE address = ? AND openfield LIKE ? ORDER BY block_height ASC LIMIT 1;"), (x,) + ("alias=" + '%',))
                            try:
                                result = h3.fetchall()[0][0]
                            except:
                                result = x
                            results.append(result)

                        connections.send(self.request, results, 10)
                    else:
                        app_log.info("{} not whitelisted for aliasgetes command".format(peer_ip))

                elif data == "txsend":
                    if (peer_ip in allowed or "any" in allowed):
                        tx_remote = connections.receive(self.request, 10)

                        # receive data necessary for remote tx construction
                        remote_tx_timestamp = tx_remote[0]
                        remote_tx_privkey = tx_remote[1]
                        remote_tx_recipient = tx_remote[2]
                        remote_tx_amount = tx_remote[3]
                        remote_tx_keep = tx_remote[4]
                        remote_tx_openfield = tx_remote[5]
                        # receive data necessary for remote tx construction

                        # derive remaining data
                        tx_remote_key = RSA.importKey(remote_tx_privkey)
                        remote_tx_pubkey = tx_remote_key.publickey().exportKey().decode("utf-8")

                        remote_tx_pubkey_hashed = base64.b64encode(remote_tx_pubkey.encode('utf-8')).decode("utf-8")

                        remote_tx_address = hashlib.sha224(remote_tx_pubkey.encode("utf-8")).hexdigest()
                        # derive remaining data

                        # construct tx
                        remote_tx = (str(remote_tx_timestamp), str(remote_tx_address), str(remote_tx_recipient), '%.8f' % float(remote_tx_amount), str(remote_tx_keep), str(remote_tx_openfield))  # this is signed

                        remote_hash = SHA.new(str(remote_tx).encode("utf-8"))
                        remote_signer = PKCS1_v1_5.new(tx_remote_key)
                        remote_signature = remote_signer.sign(remote_hash)
                        remote_signature_enc = base64.b64encode(remote_signature).decode("utf-8")
                        # construct tx

                        # insert to mempool, where everything will be verified
                        mempool_data = [((str(remote_tx_timestamp), str(remote_tx_address), str(remote_tx_recipient), '%.8f' % float(remote_tx_amount), str(remote_signature_enc), str(remote_tx_pubkey_hashed), str(remote_tx_keep), str(remote_tx_openfield)))]

                        mempool_merge(mempool_data, peer_ip, c, mempool, m)
                        connections.send(self.request, str(remote_signature_enc), 10)
                        # wipe variables
                        (tx_remote, remote_tx_privkey, tx_remote_key) = (None, None, None)
                    else:
                        app_log.info("{} not whitelisted for txsend command".format(peer_ip))

                # less importent methods
                elif data == "addvalidate":
                    if (peer_ip in allowed or "any" in allowed):

                        address_validate = connections.receive(self.request, 10)
                        if len(address_validate) == 56 and not re.search("[^abcdef0123456789]", address_validate):
                            result = "valid"
                        else:
                            result = "invalid"

                        connections.send(self.request, result, 10)
                    else:
                        app_log.info("{} not whitelisted for addvalidate command".format(peer_ip))


                elif data == "peersget":
                    if (peer_ip in allowed or "any" in allowed):

                        with open(peerlist, "r") as peer_list:
                            peers_file = peer_list.read()
                        connections.send(self.request, peers_file , 10)

                    else:
                        app_log.info("{} not whitelisted for peersget command".format(peer_ip))

                elif data == "statusget":
                    if (peer_ip in allowed or "any" in allowed):
                        global consensus
                        global consensus_percentage

                        nodes_count = (len(consensus_blockheight_list))
                        nodes_list =  peer_ip_list
                        threads_count = threading.active_count()
                        uptime = int(time.time() - startup_time)

                        if reveal_address == "yes":
                            revealed_address = address
                        else:
                            revealed_address = "private"

                        connections.send(self.request, (revealed_address, nodes_count, nodes_list, threads_count, uptime, consensus, consensus_percentage, VERSION), 10)

                    else:
                        app_log.info("{} not whitelisted for statusget command".format(peer_ip))

                elif data == "diffget":
                    if (peer_ip in allowed or "any" in allowed):
                        diff = difficulty(c)
                        connections.send(self.request, diff, 10)
                    else:
                        app_log.info("{} not whitelisted for diffget command".format(peer_ip))

                else:
                    raise ValueError("Unexpected error, received: " + str(data))

                if not time.time() <= timer_operation + timeout_operation:
                    timer_operation = time.time()  # reset timer
                #time.sleep(float(pause_conf))  # prevent cpu overload
                app_log.info("Server loop finished for {}".format(peer_ip))

            except Exception as e:
                app_log.info("Inbound: Lost connection to {}".format(peer_ip))
                app_log.info("Inbound: {}".format(e))

                # remove from consensus (connection from them)
                consensus_remove(peer_ip)
                # remove from consensus (connection from them)
                if self.request:
                    self.request.close()
                if debug_conf == 1:
                    raise  # major debug client
                else:
                    return

            finally:
                if mempool:
                    mempool.close()
                if conn:
                    conn.close()


# client thread
# if you "return" from the function, the exception code will node be executed and client thread will hand
def worker(HOST, PORT):
    timeout_operation = 60  # timeout
    timer_operation = time.time()  # start counting

    try:
        this_client = (HOST + ":" + str(PORT))
        s = socks.socksocket()
        if tor_conf == 1:
            s.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
        # s.setblocking(0)
        s.connect((HOST, PORT))
        app_log.info("Outbound: Connected to {}".format(this_client))

        # communication starter

        connections.send(s, "version", 10)
        connections.send(s, version, 10)

        data = connections.receive(s, 10)

        if (data == "ok"):
            app_log.info("Outbound: Node protocol version of {} matches our client".format(this_client))
        else:
            raise ValueError("Outbound: Node protocol version of {} mismatch".format(this_client))

        connections.send(s, "hello", 10)

        # communication starter

    except Exception as e:
        app_log.info("Could not connect to {}: {}".format(this_client, e))
        return  # can return here, because no lists are affected yet

    banned = 0
    peer_ip = s.getpeername()[0]
    if peer_ip not in banlist:
        banned = 0
    else:
        banned = 1
        s.close()
        app_log.warning("IP {} banned, disconnected".format(peer_ip))

    while banned == 0:
        try:
            if this_client not in connection_pool:
                connection_pool.append(this_client)
                app_log.warning("Connected to {}".format(this_client))
                app_log.info("Current active pool: {}".format(connection_pool))

            hdd2, h2 = db_h2_define()
            mempool, m = db_m_define()
            backup, b = db_b_define()
            conn, c = db_c_define()

            if full_ledger == 1:
                hdd, h = db_h_define()
                h3 = h
            else:
                hdd, h = None, None
                h3 = h2

            data = connections.receive(s, 10)  # receive data, one and the only root point
            #print(data)

            if data == "peers":
                subdata = connections.receive(s, 10)

                if peersync_lock.locked() == False and accept_peers == "yes":
                    peersync_lock.acquire()

                    # get remote peers into tuples (actually list)
                    server_peer_tuples = re.findall("'([\d\.]+)', '([\d]+)'", subdata)
                    app_log.info("Received following {} peers: {}".format(len((server_peer_tuples)), server_peer_tuples))
                    # get remote peers into tuples (actually list)

                    # get local peers into tuples
                    peer_file = open(peerlist, 'r')
                    peer_tuples = []
                    for line in peer_file:
                        extension = re.findall("'([\d\.]+)', '([\d]+)'", line)
                        peer_tuples.extend(extension)
                    peer_file.close()
                    # get local peers into tuples

                    for x in set(server_peer_tuples):  # set removes duplicates
                        if x not in peer_tuples:
                            app_log.info("Outbound: {} is a new peer, saving if connectible".format(x))
                            try:
                                s_purge = socks.socksocket()
                                s_purge.settimeout(0.2)
                                if tor_conf == 1:
                                    s_purge.settimeout(5)
                                    s_purge.setproxy(socks.PROXY_TYPE_SOCKS5, "127.0.0.1", 9050)
                                    # s_purge = s.setblocking(0)

                                s_purge.connect((x[0], int(x[1])))  # save a new peer file with only active nodes
                                s_purge.close()

                                peer_list_file = open(peerlist, 'a')
                                peer_list_file.write("('" + x[0] + "', '" + x[1] + "')\n")
                                peer_list_file.close()
                            except:
                                pass
                                app_log.info("Not connectible")

                        else:
                            app_log.info("Outbound: {} is not a new peer".format(x))
                    peersync_lock.release()
                else:
                    app_log.info("Outbound: Peer sync occupied")


            elif data == "sync":
                if not time.time() <= timer_operation + timeout_operation:
                    timer_operation = time.time()  # reset timer

                try:

                    global syncing

                    while len(syncing) >= 3:
                        time.sleep(int(pause_conf))

                    syncing.append(peer_ip)
                    # sync start

                    # send block height, receive block height
                    connections.send(s, "blockheight", 10)

                    execute(c, ('SELECT block_height FROM transactions ORDER BY block_height DESC LIMIT 1'))
                    db_block_height = c.fetchone()[0]

                    app_log.info("Outbound: Sending block height to compare: {}".format(db_block_height))
                    # append zeroes to get static length
                    connections.send(s, db_block_height, 10)

                    received_block_height = connections.receive(s, 10)  # receive node's block height
                    app_log.info("Outbound: Node {} is at block height: {}".format(peer_ip, received_block_height))

                    if int(received_block_height) < db_block_height:
                        app_log.warning("Outbound: We have a higher block ({}) than {} ({}), sending".format(db_block_height,peer_ip,received_block_height))

                        data = connections.receive(s, 10)  # receive client's last block_hash

                        # send all our followup hashes
                        app_log.info("Outbound: Will seek the following block: {}".format(data))

                        # consensus pool 2 (active connection)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        consensus_add(peer_ip, consensus_blockheight)
                        # consensus pool 2 (active connection)

                        try:
                            execute_param(h3, ("SELECT block_height FROM transactions WHERE block_hash = ?;"), (data,))
                            client_block = h3.fetchone()[0]

                            app_log.info("Outbound: Node is at block {}".format(client_block))  # now check if we have any newer

                            execute(h3, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                            db_block_hash = h3.fetchone()[0]  # get latest block_hash

                            if db_block_hash == data:
                                app_log.info("Outbound: Node has the latest block")
                                connections.send(s, "nonewblk", 10)

                            else:
                                execute_param(h3, ("SELECT block_height, timestamp,address,recipient,amount,signature,public_key,keep,openfield FROM transactions WHERE block_height > ? AND block_height < ?;"),
                                              (str(int(client_block)),) + (str(int(client_block + 1500)),))  # select Inbound transaction + 1, only columns that need not be verified
                                blocks_fetched = h3.fetchall()
                                # hdd.close()

                                blocks_send = [[l[1:] for l in group] for _, group in groupby(blocks_fetched, key=itemgetter(0))]  # remove block number

                                # app_log.info("Outbound: Selected " + str(blocks_send) + " to send")

                                connections.send(s, "blocksfnd", 10)

                                confirmation = connections.receive(s, 10)

                                if confirmation == "blockscf":
                                    app_log.info("Outbound: Client confirmed they want to sync from us")
                                    connections.send(s, blocks_send, 10)

                                elif confirmation == "blocksrj":
                                    app_log.info("Outbound: Client rejected to sync from us because we're dont have the latest block")
                                    pass

                        except:
                            app_log.info("Outbound: Block not found")
                            connections.send(s, "blocknf", 10)
                            connections.send(s, data, 10)

                    elif int(received_block_height) >= db_block_height:
                        if int(received_block_height) == db_block_height:
                            app_log.info("Outbound: We have the same block as {} ({}), hash will be verified".format(peer_ip,received_block_height))
                        else:
                            app_log.warning("Outbound: We have a lower block ({}) than {} ({}), hash will be verified".format(db_block_height,peer_ip,received_block_height))

                        execute(c, ('SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1'))
                        db_block_hash = c.fetchone()[0]  # get latest block_hash

                        app_log.info("Outbound: block_hash to send: {}".format(db_block_hash))
                        connections.send(s, db_block_hash, 10)

                        # consensus pool 2 (active connection)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        consensus_add(peer_ip, consensus_blockheight)
                        # consensus pool 2 (active connection)

                except Exception as e:
                    app_log.info("Outbound: Sync failed {}".format(e))
                finally:
                    syncing.remove(peer_ip)

            elif data == "blocknf":
                block_hash_delete = connections.receive(s, 10)
                # print peer_ip
                if max(consensus_blockheight_list) == int(received_block_height):
                    blocknf(block_hash_delete, peer_ip, conn, c, hdd, h, hdd2, h2, backup, b)
                    if warning(s, peer_ip, "Rollback",2) == "banned":
                        raise ValueError("{} is banned".format(peer_ip))

                while db_lock.locked() == True:
                    time.sleep(float(pause_conf))
                connections.send(s, "sendsync", 10)

            elif data == "blocksfnd":
                app_log.info("Outbound: Node {} has the block(s)".format(peer_ip))  # node should start sending txs in this step

                # app_log.info("Inbound: Combined segments: " + segments)
                # print peer_ip
                if db_lock.locked() == True:
                    app_log.warning("Skipping sync from {}, syncing already in progress".format(peer_ip))

                else:
                    execute(c, "SELECT timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")  # or it takes the first
                    last_block_ago = float(c.fetchone()[0])

                    if int(last_block_ago) < (time.time() - 600):
                        block_req = most_common(consensus_blockheight_list)
                        app_log.warning("Most common block rule triggered")
                    else:
                        block_req = max(consensus_blockheight_list)
                        app_log.warning("Longest chain rule triggered")

                    if int(received_block_height) >= block_req:
                        app_log.warning("Confirming to sync from {}".format(peer_ip))

                        try:  # they claim to have the longest chain, things must go smooth or ban
                            connections.send(s, "blockscf", 10)
                            segments = connections.receive(s, 10)
                        except:
                            if warning(s, peer_ip, "Failed to deliver the longest chain", 10) == "banned":
                                raise ValueError("{} is banned".format(peer_ip))

                        if db_lock.locked() == False:  # second check for lock
                            digest_block(segments, s, peer_ip, conn, c, mempool, m, hdd, h, hdd2, h2, h3)
                        else:
                            app_log.warning("Skipping block processing from {}, someone delivered data faster".format(peer_ip))
                        # receive theirs
                    else:
                        connections.send(s, "blocksrj", 10)
                        app_log.warning("Inbound: Distant peer {} is at {}, should be at least {}".format(peer_ip, received_block_height, block_req))

                connections.send(s, "sendsync", 10)

                # block_hash validation end

            elif data == "nonewblk":
                # sand and receive mempool
                execute(m, ('SELECT * FROM transactions'))
                mempool_txs = m.fetchall()

                # app_log.info("Outbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                # send own
                connections.send(s, "mempool", 10)
                connections.send(s, mempool_txs, 10)
                # send own

                # receive theirs
                segments = connections.receive(s, 10)
                mempool_merge(segments, peer_ip, c, mempool, m)
                # receive theirs

                # receive mempool

                app_log.info("Outbound: Synchronization with {} finished".format(peer_ip))

                time.sleep(float(pause_conf))
                while db_lock.locked() == True:
                    time.sleep(float(pause_conf))

                connections.send(s, "sendsync", 10)

            else:
                raise ValueError("Unexpected error, received: {}".format(data))

        except Exception as e:
            # remove from active pool
            if this_client in connection_pool:
                app_log.info("Will remove {} from active pool {}".format(this_client, connection_pool))
                app_log.warning("Outbound: Disconnected from {}: {}".format(this_client, e))
                connection_pool.remove(this_client)
            # remove from active pool

            # remove from consensus 2
            try:
                consensus_remove(peer_ip)
            except:
                pass
            # remove from consensus 2

            app_log.info("Connection to {} terminated due to {}".format(this_client, e))
            app_log.info("---thread {} ended---".format(threading.currentThread()))

            # properly end the connection
            if s:
                s.close()
            # properly end the connection
            if debug_conf == 1:
                raise  # major debug client
            else:
                app_log.info("Ending thread, because {}".format(e))
                return

        finally:
            try:
                mempool.close()
            except:
                pass
            try:
                conn.close()
            except:
                pass


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass


if __name__ == "__main__":
    try:
        purge_old_peers()
        if tor_conf == 0:
            # Port 0 means to select an arbitrary unused port
            HOST, PORT = "0.0.0.0", int(port)

            server = ThreadedTCPServer((HOST, PORT), ThreadedTCPRequestHandler)
            ip, port = server.server_address

            # Start a thread with the server -- that thread will then start one
            # more thread for each request

            server_thread = threading.Thread(target=server.serve_forever)

            # Exit the server thread when the main thread terminates

            server_thread.daemon = True
            server_thread.start()
            app_log.warning("Server loop running in thread: {}".format(server_thread.name))
        else:
            app_log.warning("Not starting a local server to conceal identity on Tor network")

        # start connection manager
        t_manager = threading.Thread(target=manager(c, conn))
        app_log.warning("Starting connection manager")
        t_manager.daemon = True
        t_manager.start()
        # start connection manager

        # server.serve_forever() #added
        server.shutdown()
        server.server_close()

    except Exception as e:
        app_log.info("Node already running?")
        app_log.info(e)
        raise
sys.exit()